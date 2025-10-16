#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Avito → FastAPI webhook → OpenAI Assistants → ответ в чат Авито.
Плюс админка:
- /admin (HTML страница)
- /api/admin/settings   GET/PUT
- /api/admin/files      GET/POST
- /api/admin/files/{id} DELETE
"""

import json
import os
import sqlite3
import time
import argparse
from io import BytesIO
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.routing import APIRouter
from dotenv import load_dotenv

# ---------- env & constants ----------

load_dotenv()

AVITO_BASE = os.getenv("AVITO_BASE_URL", "https://api.avito.ru")
AVITO_CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")
AVITO_ACCOUNT_ID = os.getenv("AVITO_ACCOUNT_ID")  # строка или int
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# В старых версиях переменная называлась ASSISTANT_ID.
# Поддержим обе, приоритет за OPENAI_ASSISTANT_ID.
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID") or os.getenv("ASSISTANT_ID")

SELLER_PROFILE = os.getenv("SELLER_PROFILE", "Вы — вежливый ассистент продавца. Коротко и по делу.")
REPLY_PREFIX = os.getenv("REPLY_PREFIX", "")
ROOT_PATH = os.getenv("ROOT_PATH", "").rstrip("/")  # например "/Cash-Cross" или ""
PORT = int(os.getenv("PORT", "8000"))

# Ленивая проверка
for var in ["AVITO_CLIENT_ID", "AVITO_CLIENT_SECRET", "OPENAI_API_KEY"]:
    if not globals().get(var):
        print(f"WARNING: {var} не задан в окружении (.env)")
if not ASSISTANT_ID:
    print("WARNING: OPENAI_ASSISTANT_ID/ASSISTANT_ID не задан — админка сможет создать/привязать Vector Store,"
          " но ID ассистента нужен для ответов.")


# ---------- OpenAI (Assistants API) ----------

from openai import OpenAI
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- simple storage (sqlite): chat_id → thread_id, settings, vector store id ----------

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
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # сюда сохраним id единственного vector store
    c.execute("""
        CREATE TABLE IF NOT EXISTS kv(
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    conn.close()

def db_get(key: str, table: str = "settings") -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"SELECT value FROM {table} WHERE key=?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def db_set(key: str, value: str, table: str = "settings") -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"INSERT INTO {table}(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
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
    th = openai_client.beta.threads.create()
    thread_id = th.id
    c.execute("INSERT INTO threads(chat_id, thread_id) VALUES(?,?)", (chat_id, thread_id))
    conn.commit()
    conn.close()
    return thread_id

# ---------- Vector Store helpers ----------

def get_vector_store_id() -> Optional[str]:
    return db_get("vector_store_id", table="kv")

def set_vector_store_id(vs_id: str) -> None:
    db_set("vector_store_id", vs_id, table="kv")

def ensure_vector_store_attached() -> Optional[str]:
    """
    Создаёт Vector Store (если нет) и прикрепляет к ассистенту в tool_resources.file_search.
    Возвращает id vector store или None, если нет ASSISTANT_ID.
    """
    if not ASSISTANT_ID:
        return None

    vs_id = get_vector_store_id()
    if not vs_id:
        vs = openai_client.beta.vector_stores.create(name="Avito Assistant Knowledge Base")
        vs_id = vs.id
        set_vector_store_id(vs_id)

    # Пробуем прочитать ассистента и обновить tool_resources
    asst = openai_client.beta.assistants.retrieve(ASSISTANT_ID)
    current_ids: List[str] = []
    try:
        current_ids = (asst.tool_resources or {}).get("file_search", {}).get("vector_store_ids", []) or []
    except Exception:
        current_ids = []

    if vs_id not in current_ids:
        new_ids = list(dict.fromkeys(current_ids + [vs_id]))
        openai_client.beta.assistants.update(
            assistant_id=ASSISTANT_ID,
            tool_resources={"file_search": {"vector_store_ids": new_ids}},
        )
    return vs_id

# ---------- Avito auth (client_credentials) ----------

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0}

def avito_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - now > 60:
        return _token_cache["access_token"]
    resp = requests.post(
        f"{AVITO_BASE}/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": AVITO_CLIENT_ID,
            "client_secret": AVITO_CLIENT_SECRET,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return _token_cache["access_token"]

def avito_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {avito_token()}"}

# ---------- Avito API helpers ----------

def avito_whoami() -> Dict[str, Any]:
    r = requests.get(f"{AVITO_BASE}/core/v1/accounts/self", headers=avito_headers(), timeout=20)
    r.raise_for_status()
    return r.json()

def avito_send_text(user_id: str, chat_id: str, text: str) -> None:
    url = f"{AVITO_BASE}/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages"
    payload = {"type": "text", "message": {"text": text}}
    r = requests.post(url, headers={**avito_headers(), "Content-Type": "application/json"}, json=payload, timeout=20)
    if r.status_code >= 400:
        print("Avito send error:", r.status_code, r.text)
    r.raise_for_status()

def avito_subscribe_webhook(url: str) -> Dict[str, Any]:
    r = requests.post(
        f"{AVITO_BASE}/messenger/v3/webhook",
        headers={**avito_headers(), "Content-Type": "application/json"},
        json={"url": url},
        timeout=20,
    )
    if r.status_code >= 400:
        print("Webhook subscribe error:", r.status_code, r.text)
    r.raise_for_status()
    return r.json()

# ---------- AI pipeline ----------

DEFAULT_INSTRUCTIONS = (
    "Ты — ассистент продавца на Авито. Отвечай кратко (1–3 предложения), "
    "без маркетинговых штампов, с уважением на «Вы». Если клиент задаёт цену — не торопись её "
    "называть, уточни детали. Если просит телефон — предложи продолжить в чате или оставить номер. "
    "Если вопрос не по теме — мягко верни к товару/услуге.\n\n"
    f"Профиль продавца:\n{SELLER_PROFILE}\n"
)

def get_effective_instructions() -> str:
    # если в админке сохранены инструкции — используем их; иначе дефолт
    saved = db_get("instructions", table="settings")
    return saved if (saved and saved.strip()) else DEFAULT_INSTRUCTIONS

def run_assistant_and_get_reply(chat_id: str, buyer_text: str, context: Optional[Dict[str, Any]] = None) -> str:
    """
    Создаём/берём thread по chat_id, добавляем сообщение пользователя, запускаем run ассистента,
    дожидаемся завершения и возвращаем последний текст.
    """
    thread_id = get_or_create_thread(chat_id)

    # Контекст объявления
    extra = ""
    if context and context.get("type") == "item":
        v = context.get("value") or {}
        extra = f'\nКонтекст объявления: "{v.get("title","")}" | Цена: {v.get("price_string","-")} | URL: {v.get("url","-")}\n'

    # 1) пользовательское сообщение
    openai_client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=f"{buyer_text}\n\n[Источник: Авито-чат {chat_id}]{extra}",
    )

    # 2) run ассистента (инструкции — из админки)
    instructions = get_effective_instructions()

    run = openai_client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        additional_instructions=instructions,
    )

    # 3) короткий поллинг
    started = time.time()
    while True:
        run = openai_client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        if run.status in ("completed", "failed", "cancelled", "expired"):
            break
        if time.time() - started > 15:
            break
        time.sleep(1.2)

    # 4) достаём последнее сообщение ассистента
    msgs = openai_client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=10)
    text_reply = ""
    for m in msgs.data:
        if m.role == "assistant":
            chunks = []
            for part in m.content:
                if part.type == "text":
                    chunks.append(part.text.value)
            if chunks:
                text_reply = "\n".join(chunks).strip()
                break

    if not text_reply:
        text_reply = "Спасибо за сообщение! Сейчас уточню детали и вернусь с ответом."

    if REPLY_PREFIX:
        text_reply = f"{REPLY_PREFIX}{text_reply}"

    return text_reply[:1000]

# ---------- FastAPI app ----------

db_init()
app = FastAPI(title="Avito AI Assistant Bot", root_path=ROOT_PATH or "")

# ---------- Webhook ----------

@app.post("/avito-webhook")
async def avito_webhook(request: Request, background: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    def handle():
        try:
            payload = data.get("payload") or {}
            if payload.get("type") != "message":
                return
            msg = payload.get("value") or {}
            chat_id = msg.get("chat_id")
            user_id = msg.get("user_id")
            author_id = msg.get("author_id")
            msg_type = msg.get("type")
            content = msg.get("content") or {}
            item_id = msg.get("item_id")
            if not chat_id or not user_id:
                return
            if author_id == user_id:
                return
            if msg_type != "text":
                return
            buyer_text = (content.get("text") or "").strip()
            if not buyer_text:
                return

            ctx = None
            if item_id:
                ctx = {"type": "item", "value": {"title": "", "price_string": "", "url": f"https://avito.ru/{item_id}"}}

            reply = run_assistant_and_get_reply(chat_id, buyer_text, context=ctx)
            avito_send_text(str(user_id), chat_id, reply)

        except Exception as e:
            print("Webhook handle error:", repr(e))

    background.add_task(handle)
    return JSONResponse({"ok": True})

@app.get("/health")
def health():
    return {"status": "ok", "root_path": ROOT_PATH or ""}

# ---------- Admin: API ----------

admin_api = APIRouter(prefix="/api/admin", tags=["admin"])

@admin_api.get("/settings")
def get_settings():
    ensure_vector_store_attached()  # создаст/привяжет при первом заходе
    instructions = get_effective_instructions()
    return {
        "assistant_id": ASSISTANT_ID,
        "vector_store_id": get_vector_store_id(),
        "instructions": instructions,
    }

@admin_api.put("/settings")
async def put_settings(payload: Dict[str, Any]):
    instructions = (payload or {}).get("instructions", "") or ""
    db_set("instructions", instructions, table="settings")
    return {"ok": True}

@admin_api.get("/files")
def list_files():
    """
    Возвращает унифицированный список файлов vector store ассистента:
    [{id, filename, bytes, created_at, status}]
    """
    vs_id = ensure_vector_store_attached()
    if not vs_id:
        return {"data": [], "warning": "assistant_id is not set"}

    # Список файлов vector store
    vs_files = openai_client.beta.vector_stores.files.list(vector_store_id=vs_id, limit=100)
    result = []
    for vsf in vs_files.data:
        status = getattr(vsf, "status", "ready")
        # Получим базовую инфу о файле
        try:
            f = openai_client.files.retrieve(vsf.file_id)
            result.append({
                "id": vsf.id,                 # id связи (vsfile_*)
                "file_id": f.id,              # file_*
                "filename": getattr(f, "filename", None),
                "bytes": getattr(f, "bytes", None),
                "created_at": getattr(f, "created_at", None),
                "status": status,
            })
        except Exception:
            # fallback если retrieve не вышел
            result.append({
                "id": vsf.id,
                "file_id": vsf.file_id,
                "filename": None,
                "bytes": None,
                "created_at": None,
                "status": status,
            })
    return {"data": result}

@admin_api.post("/files")
async def upload_files(files: List[UploadFile] = File(...)):
    """
    Загрузка файлов в vector store.
    """
    vs_id = ensure_vector_store_attached()
    if not vs_id:
        return JSONResponse({"error": "assistant_id is not set"}, status_code=400)

    created = []
    for uf in files:
        content = await uf.read()
        # 1) создаём File
        f = openai_client.files.create(
            file=(uf.filename, BytesIO(content)),
            purpose="assistants",
        )
        # 2) прикрепляем к Vector Store
        vsf = openai_client.beta.vector_stores.files.create(
            vector_store_id=vs_id,
            file_id=f.id,
        )
        created.append({"file_id": f.id, "vsfile_id": vsf.id, "filename": uf.filename})
    return {"uploaded": created}

@admin_api.delete("/files/{any_id}")
def delete_file(any_id: str):
    """
    Удаление: пытаемся как связь vector store file, если не вышло — как file_*.
    """
    vs_id = get_vector_store_id()
    ok = False
    err = None
    if vs_id:
        try:
            openai_client.beta.vector_stores.files.delete(vector_store_id=vs_id, file_id=any_id)
            ok = True
        except Exception as e:
            err = repr(e)
    if not ok:
        try:
            openai_client.files.delete(any_id)
            ok = True
        except Exception as e:
            err = repr(e)
    if not ok:
        return JSONResponse({"ok": False, "error": err or "not deleted"}, status_code=400)
    return {"ok": True}

app.include_router(admin_api)

# ---------- Admin: HTML ----------

ADMIN_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Админка — Avito Assistant</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #0b1220;
      --panel: #121a2b;
      --text: #e8eefc;
      --muted: #9fb3d9;
      --accent: #F58220;
      --bad: #ff6b6b;
      --good: #22c55e;
      --border: #1f2a44;
    }
    * { box-sizing: border-box; }
    body {
      margin:0; background:var(--bg); color:var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Noto Sans, Arial, "Apple Color Emoji","Segoe UI Emoji";
    }
    .wrap { max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    h1 { font-size: 20px; margin: 0 0 16px; }
    h2 { font-size: 16px; margin: 24px 0 8px; color: var(--muted); }
    .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
    @media (min-width: 900px){ .grid { grid-template-columns: 1.2fr .8fr; } }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px; padding: 16px;
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
    }
    textarea, input[type="text"] {
      width: 100%; background: #0f1626; border:1px solid var(--border); color: var(--text);
      border-radius: 10px; padding: 10px 12px; outline:none; font-size:14px;
    }
    textarea { min-height: 160px; resize: vertical; }
    button {
      appearance:none; border:1px solid transparent;
      background: var(--accent); color: #000; font-weight: 600;
      padding: 10px 14px; border-radius: 10px; cursor: pointer;
    }
    button.secondary { background: transparent; border-color: var(--border); color: var(--text); }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .row { display:flex; gap:8px; align-items:center; flex-wrap: wrap; }
    .muted { color: var(--muted); font-size: 12px; }
    .table {
      width: 100%; border-collapse: collapse; font-size: 14px; overflow: hidden; border-radius: 10px;
      border:1px solid var(--border);
    }
    .table th, .table td { padding:10px 12px; text-align: left; }
    .table thead { background: #0f1626; color: var(--muted); }
    .table tr + tr td { border-top:1px solid var(--border); }
    .tag { display:inline-block; padding:2px 8px; border-radius: 999px; font-size: 12px; }
    .tag.good { background: rgba(34,197,94,.15); color: #22c55e; }
    .tag.bad  { background: rgba(255,107,107,.15); color: #ff8484; }
    .tag.wait { background: rgba(245,130,32,.15); color: var(--accent); }
    .toolbar { display:flex; justify-content: space-between; gap: 12px; align-items: center; }
    .right { text-align: right; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace; }
    .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
    .error { color: var(--bad); white-space: pre-wrap; }
    .ok    { color: var(--good); }
    @media (max-width: 640px) {
      .wrap { margin: 16px auto; padding: 0 12px; }
      h1 { font-size: 18px; }
      h2 { font-size: 15px; }
      .toolbar { flex-wrap: wrap; gap: 8px; }
      .toolbar .muted { width: 100%; order: 2; }
      #filesWrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 10px; }
      #filesWrap::-webkit-scrollbar { height: 8px; }
      #filesWrap::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; }
      .table { min-width: 640px; font-size: 13px; }
      .table th, .table td { padding: 8px 10px; }
      #filesWrap thead th { position: sticky; top: 0; z-index: 1; background: #0f1626; }
      .table td:first-child { word-break: break-all; }
      button { padding: 9px 12px; }
    }
    @media (min-width: 641px) and (max-width: 900px) {
      #filesWrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
      .table { min-width: 700px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="toolbar">
      <h1>Админка — Avito Assistant</h1>
      <div class="muted">API base: <span class="mono" id="apiBase"></span></div>
    </div>

    <div class="grid">
      <div class="card">
        <h2>Инструкция ассистента</h2>
        <textarea id="instructions" placeholder="Инструкция ассистента..."></textarea>
        <div class="row" style="margin-top:10px;">
          <button id="saveSettingsBtn">Сохранить</button>
          <span class="hint" id="settingsMsg"></span>
        </div>
      </div>

      <div class="card">
        <h2>Загрузка файлов в базу знаний</h2>
        <input id="fileInput" type="file" multiple />
        <div class="row" style="margin-top:10px;">
          <button id="uploadBtn">Загрузить</button>
          <span class="hint" id="uploadMsg"></span>
        </div>
        <div class="hint" style="margin-top:8px;">
          Поддерживаются форматы из <a href="https://platform.openai.com/docs/assistants/tools/file-search/supported-files" target="_blank" rel="noreferrer">документации</a>.
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px;">
      <div class="toolbar">
        <h2>Файлы в Vector Store</h2>
        <button class="secondary" id="reloadBtn">Обновить список</button>
      </div>
      <div id="filesWrap">
        <table class="table">
          <thead>
            <tr>
              <th>Имя файла</th>
              <th class="right">Размер</th>
              <th>Статус</th>
              <th>Создан</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="filesTbody">
            <tr><td colspan="5" class="muted">Загрузка...</td></tr>
          </tbody>
        </table>
        <div class="hint" id="filesMsg" style="margin-top:8px;"></div>
      </div>
    </div>
  </div>

<script>
(function(){
  // сервер подставит правильный префикс (учитывает ROOT_PATH)
  const ROOT = "%ROOT_PATH%";
  const API = ROOT + "/api/admin";
  document.getElementById('apiBase').textContent = API;

  const $ = (id)=>document.getElementById(id);
  const fmtBytes = (n)=> n!=null ? new Intl.NumberFormat('ru-RU').format(n) + ' B' : '—';
  const fmtDate  = (ts)=> ts ? new Date(ts*1000).toLocaleString() : '—';

  async function jsonFetch(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      const t = await r.text().catch(()=> '');
      throw new Error(`${r.status} ${t || r.statusText}`);
    }
    const ct = r.headers.get("content-type") || "";
    if (ct.includes("application/json")) return r.json();
    return r.text();
  }

  // --- SETTINGS ---
  async function loadSettings() {
    $('settingsMsg').textContent = '';
    try {
      const s = await jsonFetch(API + '/settings');
      $('instructions').value = s.instructions || '';
    } catch(e) {
      $('settingsMsg').textContent = 'Ошибка: ' + e.message;
      $('settingsMsg').className = 'hint error';
    }
  }

  async function saveSettings() {
    $('settingsMsg').textContent = '';
    try {
      const instructions = $('instructions').value || '';
      await jsonFetch(API + '/settings', {
        method:'PUT',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ instructions })
      });
      $('settingsMsg').textContent = 'Сохранено';
      $('settingsMsg').className = 'hint ok';
    } catch(e) {
      $('settingsMsg').textContent = 'Ошибка: ' + e.message;
      $('settingsMsg').className = 'hint error';
    }
  }

  // --- FILES LIST/UPLOAD/DELETE ---
  async function loadFiles() {
    $('filesMsg').textContent = '';
    const tbody = $('filesTbody');
    tbody.innerHTML = '<tr><td colspan="5" class="muted">Загрузка...</td></tr>';
    try {
      const data = await jsonFetch(API + '/files');
      const rows = Array.isArray(data.data) ? data.data : [];
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="muted">Пока файлов нет</td></tr>';
        return;
      }
      tbody.innerHTML = '';
      for (const row of rows) {
        const tr = document.createElement('tr');

        const filename = row.filename || row.name || row.file_id || row.id;
        const size = row.bytes ?? row.usage_bytes;
        const status = (row.status || 'ready');

        const tdName = document.createElement('td');
        tdName.textContent = filename;

        const tdSize = document.createElement('td');
        tdSize.className = 'right';
        tdSize.textContent = fmtBytes(size);

        const tdStatus = document.createElement('td');
        const span = document.createElement('span');
        span.className = 'tag ' + (status==='completed' || status==='ready' ? 'good' : (status==='failed' ? 'bad' : 'wait'));
        span.textContent = status;
        tdStatus.appendChild(span);

        const tdDt = document.createElement('td');
        tdDt.textContent = fmtDate(row.created_at);

        const tdAct = document.createElement('td');
        const btn = document.createElement('button');
        btn.textContent = 'Удалить';
        btn.className = 'secondary';
        btn.onclick = async () => {
          if (!confirm(`Удалить файл "${filename}"?`)) return;
          try {
            const id = encodeURIComponent(row.id || row.file_id);
            await jsonFetch(API + '/files/' + id, { method:'DELETE' });
            await loadFiles();
          } catch(e) {
            $('filesMsg').textContent = 'Ошибка удаления: ' + e.message;
            $('filesMsg').className = 'hint error';
          }
        };
        tdAct.appendChild(btn);

        tr.appendChild(tdName);
        tr.appendChild(tdSize);
        tr.appendChild(tdStatus);
        tr.appendChild(tdDt);
        tr.appendChild(tdAct);
        tbody.appendChild(tr);
      }
    } catch(e) {
      $('filesMsg').textContent = 'Ошибка загрузки: ' + e.message;
      $('filesMsg').className = 'hint error';
    }
  }

  async function uploadFiles() {
    $('uploadMsg').textContent = '';
    const input = $('fileInput');
    if (!input.files || !input.files.length) {
      $('uploadMsg').textContent = 'Выберите файлы';
      $('uploadMsg').className = 'hint error';
      return;
    }
    const fd = new FormData();
    for (const f of input.files) fd.append('files', f, f.name);
    try {
      await jsonFetch(API + '/files', { method:'POST', body: fd });
      $('uploadMsg').textContent = 'Файлы отправлены. Обновляю список...';
      $('uploadMsg').className = 'hint ok';
      input.value = '';
      setTimeout(loadFiles, 600);
    } catch(e) {
      $('uploadMsg').textContent = 'Ошибка: ' + e.message;
      $('uploadMsg').className = 'hint error';
    }
  }

  // bind
  $('saveSettingsBtn').onclick = saveSettings;
  $('uploadBtn').onclick = uploadFiles;
  $('reloadBtn').onclick = loadFiles;

  // init
  loadSettings();
  loadFiles();
})();
</script>
</body>
</html>
"""

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    html = ADMIN_HTML.replace("%ROOT_PATH%", ROOT_PATH or "")
    return HTMLResponse(content=html, status_code=200)

# ---------- CLI ----------

def cmd_subscribe(url: str):
    res = avito_subscribe_webhook(url)
    print(json.dumps(res, ensure_ascii=False, indent=2))

def cmd_whoami():
    me = avito_whoami()
    print(json.dumps(me, ensure_ascii=False, indent=2))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Запустить HTTP-сервер (FastAPI/uvicorn)")
    parser.add_argument("--subscribe", metavar="URL", help="Подписать Avito webhook на заданный URL")
    parser.add_argument("--whoami", action="store_true", help="Пробный запрос /core/v1/accounts/self")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.subscribe:
        cmd_subscribe(args.subscribe)
        return
    if args.whoami:
        cmd_whoami()
        return
    if args.serve:
        import uvicorn
        uvicorn.run("avito_ai_assistant_bot:app",
                    host=args.host, port=args.port,
                    reload=False, proxy_headers=True, forwarded_allow_ips="*")
        return

    parser.print_help()

if __name__ == "__main__":
    main()
