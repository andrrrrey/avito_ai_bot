#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Avito → FastAPI webhook → OpenAI Assistants → ответ в чат Авито.

Запуск:
  source .venv/bin/activate
  pip install fastapi uvicorn requests python-dotenv "openai==1.*" python-multipart
  python3 avito_ai_assistant_bot.py --serve --host 127.0.0.1 --port 8081

Подписка на вебхук:
  python3 avito_ai_assistant_bot.py --subscribe https://dev.futuguru.com/Cash-Cross/avito-webhook

Переменные окружения (.env):
  AVITO_CLIENT_ID=...
  AVITO_CLIENT_SECRET=...
  OPENAI_API_KEY=sk-...
  OPENAI_ASSISTANT_ID=asst_...     # или ASSISTANT_ID=...
  VECTOR_STORE_ID=vs_...           # если используете File Search с Vector Store
  SELLER_PROFILE="..."              # ИЛИ разложить:
  SELLER_PROFILE_NAME="..."
  SELLER_PROFILE_ABOUT="..."
  SELLER_PROFILE_RULES="..."
  SELLER_PROFILE_FAQ="..."
  ROOT_PATH=/Cash-Cross
  PORT=8081
  REPLY_PREFIX="[Авито] "           # опционально
"""

import argparse
import json
import os
import sqlite3
import time
import traceback
from typing import Any, Dict, Optional, List

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# ---------- env & constants ----------

load_dotenv()

AVITO_BASE = os.getenv("AVITO_BASE_URL", "https://api.avito.ru")
AVITO_CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID") or os.getenv("ASSISTANT_ID")
VECTOR_STORE_ID = (os.getenv("VECTOR_STORE_ID") or "").strip()

REPLY_PREFIX = os.getenv("REPLY_PREFIX", "")
ROOT_PATH = (os.getenv("ROOT_PATH") or "").rstrip("/")
PORT = int(os.getenv("PORT", "8081"))

# Профиль продавца
SELLER_PROFILE = os.getenv("SELLER_PROFILE")
if not SELLER_PROFILE:
    name = os.getenv("SELLER_PROFILE_NAME", "").strip()
    about = os.getenv("SELLER_PROFILE_ABOUT", "").strip()
    rules = os.getenv("SELLER_PROFILE_RULES", "").strip()
    faq = os.getenv("SELLER_PROFILE_FAQ", "").strip()
    parts = []
    if name:
        parts.append(f"Название/бренд: {name}")
    if about:
        parts.append(f"О нас: {about}")
    if rules:
        parts.append("Правила общения:\n" + rules)
    if faq:
        parts.append("FAQ:\n" + faq)
    SELLER_PROFILE = "\n\n".join(parts) or "Вы — вежливый ассистент продавца. Отвечайте кратко и по делу."

# ---------- OpenAI client ----------

from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

def ensure_assistant_id() -> str:
    """
    Возвращает готовый assistant_id. Если в env нет — создаёт ассистента и кэширует в assistant_id.txt.
    """
    global ASSISTANT_ID
    if ASSISTANT_ID:
        return ASSISTANT_ID

    # попробовать из файла
    aid_path = os.path.join(os.path.dirname(__file__), "assistant_id.txt")
    if os.path.exists(aid_path):
        with open(aid_path, "r", encoding="utf-8") as f:
            ASSISTANT_ID = f.read().strip()
            if ASSISTANT_ID:
                print(f"[assistant] use cached id: {ASSISTANT_ID}")
                return ASSISTANT_ID

    # создать нового
    print("[assistant] creating new assistant…")
    instr = (
        "Ты — ассистент продавца на Авито. Отвечай кратко (1–3 предложения), "
        "вежливо на «Вы», без воды. Если про цену — уточни детали. "
        "Если не по теме — мягко верни к услуге.\n\n"
        f"Профиль продавца:\n{SELLER_PROFILE}\n"
    )
    tools = []
    tool_resources = {}
    if VECTOR_STORE_ID:
        tools.append({"type": "file_search"})
        tool_resources = {"file_search": {"vector_store_ids": [VECTOR_STORE_ID]}}

    asst = openai_client.beta.assistants.create(
        name="Avito Seller Assistant",
        instructions=instr,
        model="gpt-4o-mini",
        tools=tools or None,
        tool_resources=tool_resources or None,
    )
    ASSISTANT_ID = asst.id
    with open(aid_path, "w", encoding="utf-8") as f:
        f.write(ASSISTANT_ID)
    print(f"[assistant] created: {ASSISTANT_ID}")
    return ASSISTANT_ID

# ---------- simple storage: chat_id -> thread_id ----------

DB_PATH = os.path.join(os.path.dirname(__file__), "threads.sqlite3")

def db_init():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS threads(
            chat_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_or_create_thread(chat_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT thread_id FROM threads WHERE chat_id=?", (chat_id,))
    row = c.fetchone()
    if row:
        conn.close()
        return row[0]
    # создать новый thread
    th = openai_client.beta.threads.create()
    thread_id = th.id
    c.execute("INSERT INTO threads(chat_id, thread_id) VALUES(?,?)", (chat_id, thread_id))
    conn.commit()
    conn.close()
    print(f"[threads] new thread for chat {chat_id}: {thread_id}")
    return thread_id

# ---------- Avito auth (client_credentials) ----------

_token: Dict[str, Any] = {"access_token": None, "exp": 0}

def avito_token() -> str:
    now = time.time()
    if _token["access_token"] and _token["exp"] - now > 60:
        return _token["access_token"]
    r = requests.post(
        f"{AVITO_BASE}/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": AVITO_CLIENT_ID,
            "client_secret": AVITO_CLIENT_SECRET,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    _token["access_token"] = data["access_token"]
    _token["exp"] = now + int(data.get("expires_in", 3600))
    return _token["access_token"]

def avito_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {avito_token()}", "Accept": "application/json"}

def avito_send_text(user_id: int | str, chat_id: str, text: str) -> None:
    url = f"{AVITO_BASE}/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages"
    payload = {"type": "text", "message": {"text": text}}
    r = requests.post(url, headers={**avito_headers(), "Content-Type": "application/json"}, json=payload, timeout=20)
    if r.status_code >= 400:
        print("[avito] send error:", r.status_code, r.text)
    r.raise_for_status()

def avito_subscribe_webhook(url: str) -> Dict[str, Any]:
    r = requests.post(
        f"{AVITO_BASE}/messenger/v3/webhook",
        headers={**avito_headers(), "Content-Type": "application/json"},
        json={"url": url},
        timeout=20,
    )
    if r.status_code >= 400:
        print("[avito] webhook subscribe error:", r.status_code, r.text)
    r.raise_for_status()
    return r.json() if r.content else {}

def avito_whoami() -> Dict[str, Any]:
    r = requests.get(f"{AVITO_BASE}/core/v1/accounts/self", headers=avito_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

# ---------- AI pipeline ----------

def build_system_instructions() -> str:
    return (
        "Ты — ассистент продавца на Авито. Отвечай кратко (1–3 предложения), вежливо на «Вы», без воды. "
        "Если спрашивают цену — уточни вводные. Если не по теме — верни к услуге.\n\n"
        f"Профиль продавца:\n{SELLER_PROFILE}\n"
    )

def run_assistant_and_get_reply(chat_id: str, buyer_text: str, ctx: Optional[Dict[str, Any]] = None) -> str:
    assistant_id = ensure_assistant_id()
    thread_id = get_or_create_thread(chat_id)

    extra = ""
    if ctx and ctx.get("type") == "item":
        v = ctx.get("value") or {}
        extra = f'\nКонтекст объявления: "{v.get("title","")}" | Цена: {v.get("price_string","-")} | URL: {v.get("url","-")}\n'

    openai_client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=f"{buyer_text}\n\n[Источник: Авито-чат {chat_id}]{extra}",
    )

    run = openai_client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
        additional_instructions=build_system_instructions(),
    )

    started = time.time()
    while True:
        run = openai_client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run.status in ("completed", "failed", "cancelled", "expired"):
            break
        if time.time() - started > 18:
            break
        time.sleep(1.2)

    msgs = openai_client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=10)
    reply = ""
    for m in msgs.data:
        if m.role == "assistant":
            chunks = []
            for part in m.content:
                if part.type == "text":
                    chunks.append(part.text.value)
            if chunks:
                reply = "\n".join(chunks).strip()
                break

    if not reply:
        reply = "Спасибо! Сейчас уточню детали и вернусь с ответом."

    import re
    # Убираем метки источников вида  
    reply = re.sub(r"【\d+:[^】]+】", "", reply).strip()

    if REPLY_PREFIX:
        reply = f"{REPLY_PREFIX}{reply}"

    return reply[:1000]

# ---------- FastAPI ----------

db_init()
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(title="Avito AI Assistant Bot", root_path=ROOT_PATH or "")

# CORS: чтобы браузерный preflight OPTIONS не отдавал 405
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # при желании сузить до своего домена
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Admin API (settings + files/VS) ----------

from fastapi import UploadFile, File, Path, HTTPException
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

admin_api = APIRouter(prefix="/api/admin", tags=["admin"])

def _get_assistant_obj():
    aid = ensure_assistant_id()
    try:
        return openai_client.beta.assistants.retrieve(aid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assistant retrieve failed: {e}")

def _ensure_vector_store() -> str:
    if not VECTOR_STORE_ID:
        raise HTTPException(status_code=400, detail="VECTOR_STORE_ID is not set in environment")
    return VECTOR_STORE_ID

# === ИНСТРУКЦИИ: читаем/пишем НАПРЯМУЮ в ассистент ===
@admin_api.get("/settings")
def admin_get_settings():
    a = _get_assistant_obj()
    return {"instructions": (a.instructions or "")}

@admin_api.put("/settings")
def admin_put_settings(payload: Dict[str, Any]):
    instructions = (payload or {}).get("instructions", "")
    aid = ensure_assistant_id()
    try:
        kwargs: Dict[str, Any] = {"instructions": instructions or ""}
        if VECTOR_STORE_ID:
            kwargs["tools"] = [{"type": "file_search"}]
            kwargs["tool_resources"] = {"file_search": {"vector_store_ids": [VECTOR_STORE_ID]}}
        openai_client.beta.assistants.update(assistant_id=aid, **kwargs)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Assistant update failed: {e}")

# ===== ФАЙЛЫ (Vector Store или Files API) =====

@admin_api.get("/files")
def list_files():
    """
    Если задан VECTOR_STORE_ID — показываем файлы ИЗ VECTOR STORE
    со статусом привязки (in_progress | completed | failed) и last_error.
    Имя/размер/время берём из Files API только как метаданные.
    Иначе — фоллбек на Files API (purpose=assistants).
    """
    try:
        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            vs_list = openai_client.beta.vector_stores.files.list(
                vector_store_id=VECTOR_STORE_ID,
                limit=100  # максимум у API
            )
            rows = []
            for item in vs_list.data:
                fid = getattr(item, "id", None) or getattr(item, "file_id", None)
                vs_status = getattr(item, "status", None) or "in_progress"
                last_error = getattr(item, "last_error", None)

                filename = fid
                size_bytes = None
                created_at = None
                try:
                    meta = openai_client.files.retrieve(fid)
                    filename = getattr(meta, "filename", filename)
                    size_bytes = getattr(meta, "bytes", None)
                    created_at = getattr(meta, "created_at", None)
                except Exception:
                    pass

                rows.append({
                    "id": fid,
                    "filename": filename,
                    "bytes": size_bytes,
                    "created_at": created_at,
                    "status": vs_status,       # статус из Vector Store
                    "last_error": last_error,  # если failed — будет подсказка
                })

            rows.sort(key=lambda x: (x.get("created_at") or 0), reverse=True)
            return {"data": rows}

        # Фоллбек: список Files API (purpose=assistants)
        files = openai_client.files.list()
        rows = []
        for f in files.data:
            if getattr(f, "purpose", "") == "assistants":
                rows.append({
                    "id": f.id,
                    "filename": f.filename,
                    "bytes": f.bytes,
                    "created_at": f.created_at,
                    "status": getattr(f, "status", "processed"),
                })
        rows.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return {"data": rows}

    except Exception as e:
        print("[admin/files list] error:", traceback.format_exc())
        return JSONResponse({"detail": f"List failed: {e}"}, status_code=500)

@admin_api.post("/files")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Загружает файлы:
      - если есть VECTOR_STORE_ID → в Vector Store (upload_and_poll)
      - иначе → в Files API (purpose=assistants)
    """
    try:
        if not files:
            return JSONResponse({"detail": "No files provided"}, status_code=400)

        uploaded = []
        for uf in files:
            content = await uf.read()
            if not content:
                continue

            if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
                fs = openai_client.beta.vector_stores.files.upload_and_poll(
                    vector_store_id=VECTOR_STORE_ID,
                    file=(uf.filename, content),
                )
                uploaded.append({
                    "id": fs.id,
                    "filename": uf.filename,
                    "target": "vector_store",
                    "status": getattr(fs, "status", None),
                    "last_error": getattr(fs, "last_error", None),
                })
            else:
                f = openai_client.files.create(
                    file=(uf.filename, content),
                    purpose="assistants",
                )
                uploaded.append({"id": f.id, "filename": uf.filename, "target": "files"})

        return {"ok": True, "uploaded": uploaded}
    except Exception as e:
        print("[admin/files upload] error:", traceback.format_exc())
        return JSONResponse({"detail": f"Upload failed: {e}"}, status_code=500)

@admin_api.delete("/files/{file_id}", response_class=PlainTextResponse)
def delete_file(file_id: str = Path(..., description="OpenAI File ID")):
    try:
        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            try:
                openai_client.beta.vector_stores.files.delete(
                    vector_store_id=VECTOR_STORE_ID,
                    file_id=file_id,
                )
            except Exception:
                pass
        openai_client.files.delete(file_id)
        return "ok"
    except Exception as e:
        print("[admin/files delete] error:", traceback.format_exc())
        return PlainTextResponse(f"Delete failed: {e}", status_code=500)

@admin_api.get("/files/{file_id}")
def inspect_file(file_id: str):
    """
    Диагностика: статус в Files API и в Vector Store (если есть).
    """
    try:
        info: Dict[str, Any] = {"file_id": file_id}

        # Files API
        try:
            f = openai_client.files.retrieve(file_id)
            info["files_api"] = {
                "id": f.id,
                "filename": getattr(f, "filename", None),
                "bytes": getattr(f, "bytes", None),
                "created_at": getattr(f, "created_at", None),
                "status": getattr(f, "status", None),
                "purpose": getattr(f, "purpose", None),
                "status_details": getattr(f, "status_details", None),
            }
        except Exception as e:
            info["files_api_error"] = str(e)

        # Vector Store link (если используется)
        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            try:
                vf = openai_client.beta.vector_stores.files.retrieve(
                    vector_store_id=VECTOR_STORE_ID,
                    file_id=file_id,
                )
                info["vector_store"] = {
                    "id": vf.id,
                    "status": getattr(vf, "status", None),
                    "last_error": getattr(vf, "last_error", None),
                }
            except Exception as e:
                info["vector_store_error"] = str(e)

        return info
    except Exception as e:
        print("[admin/files inspect] error:", traceback.format_exc())
        return JSONResponse({"detail": f"Inspect failed: {e}"}, status_code=500)

# Роутер админки
app.include_router(admin_api)

@app.get("/health")
def health():
    return {"status": "ok", "root_path": ROOT_PATH or ""}

@app.post("/avito-webhook")
async def avito_webhook(request: Request, background: BackgroundTasks):
    """
    Avito messenger v3 webhook:
    { id, timestamp, version, payload:{ type:"message", value:{...} } }
    Отвечаем только на входящий текст от клиента (author_id != user_id).
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    # Лог входящего события — полезно для отладки
    try:
        payload = data.get("payload") or {}
        if payload.get("type") == "message":
            val = payload.get("value") or {}
            print(f"[webhook] chat={val.get('chat_id')} author={val.get('author_id')} "
                  f"user_id={val.get('user_id')} type={val.get('type')} "
                  f"text={((val.get('content') or {}).get('text') or '')[:160]}")
        else:
            print(f"[webhook] non-message: {payload.get('type')}")
    except Exception as e:
        print("[webhook] log error:", e)

    def handle():
        try:
            payload = data.get("payload") or {}
            if payload.get("type") != "message":
                return
            msg = payload.get("value") or {}

            chat_id = msg.get("chat_id")
            user_id = msg.get("user_id")          # наш аккаунт
            author_id = msg.get("author_id")      # отправитель (клиент)
            msg_type = msg.get("type")            # "text", ...
            chat_type = msg.get("chat_type")      # "u2i"/"u2u"
            content = msg.get("content") or {}
            item_id = msg.get("item_id")

            # фильтры: только входящий текст
            if not chat_id or not user_id:
                return
            if author_id == user_id:
                return
            if msg_type != "text":
                return
            buyer_text = (content.get("text") or "").strip()
            if not buyer_text:
                return

            # минимальный контекст объявления (если есть item_id)
            ctx = None
            if item_id:
                ctx = {"type": "item", "value": {"title": "", "price_string": "", "url": f"https://avito.ru/{item_id}"}}

            reply = run_assistant_and_get_reply(chat_id, buyer_text, ctx)

            # отправляем ответ
            avito_send_text(user_id, chat_id, reply)
            print(f"[reply] -> chat={chat_id} ok")

        except Exception as e:
            print("[webhook] handle error:", repr(e))

    background.add_task(handle)
    return JSONResponse({"ok": True})

# ---------- CLI ----------

def cmd_subscribe(url: str):
    res = avito_subscribe_webhook(url)
    print(json.dumps(res, ensure_ascii=False, indent=2) if res else "{}")

def cmd_whoami():
    print(json.dumps(avito_whoami(), ensure_ascii=False, indent=2))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Запустить HTTP-сервер (FastAPI/uvicorn)")
    parser.add_argument("--subscribe", metavar="URL", help="Подписать Avito webhook на URL")
    parser.add_argument("--whoami", action="store_true", help="Проверка /core/v1/accounts/self")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.subscribe:
        cmd_subscribe(args.subscribe); return
    if args.whoami:
        cmd_whoami(); return
    if args.serve:
        import uvicorn
        uvicorn.run("avito_ai_assistant_bot:app",
                    host=args.host, port=args.port,
                    reload=False, proxy_headers=True, forwarded_allow_ips="*")
        return

    parser.print_help()

if __name__ == "__main__":
    main()
