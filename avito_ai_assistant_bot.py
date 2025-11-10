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

  # AmoCRM:
  AMOCRM_BASE_URL=https://<subdomain>.amocrm.ru
  AMOCRM_CLIENT_ID=...
  AMOCRM_CLIENT_SECRET=...
  AMOCRM_REDIRECT_URI=https://<host>/admin/amocrm/oauth/callback
  AMOCRM_ACCESS_TOKEN=
  AMOCRM_REFRESH_TOKEN=<ваш refresh_token>
  AMOCRM_PIPELINE_ID=...
  AMOCRM_STATUS_ID=...
  AMOCRM_RESPONSIBLE_USER_ID=...
  AMOCRM_TOKEN_FILE=/home/bots/novikov_avito/amocrm_token.json
"""

import argparse
import json
import os
import sqlite3
import time
import traceback
from datetime import datetime
from typing import Any, Dict, Optional, List

import re
from pathlib import Path as FsPath
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

# ---------- env & constants ----------

load_dotenv()

AVITO_BASE = os.getenv("AVITO_BASE_URL", "https://api.avito.ru")
AVITO_CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")
AVITO_ACCOUNT_ID = (os.getenv("AVITO_ACCOUNT_ID") or "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID") or os.getenv("ASSISTANT_ID")
VECTOR_STORE_ID = (os.getenv("VECTOR_STORE_ID") or "").strip()

REPLY_PREFIX = os.getenv("REPLY_PREFIX", "")
ROOT_PATH = (os.getenv("ROOT_PATH") or "").rstrip("/")
PORT = int(os.getenv("PORT", "8081"))
BOT_ENABLED = (os.getenv("BOT_ENABLED") or "1").strip().lower() not in {"0", "false", "no", "off"}
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("AMOCRM_PUBLIC_BASE_URL") or "").strip()
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or "").strip()
if not PUBLIC_BASE_URL and WEBHOOK_URL:
    parsed_webhook = urlparse(WEBHOOK_URL)
    if parsed_webhook.scheme and parsed_webhook.netloc:
        PUBLIC_BASE_URL = f"{parsed_webhook.scheme}://{parsed_webhook.netloc}"
PUBLIC_BASE_URL = PUBLIC_BASE_URL.rstrip("/")

# AmoCRM credentials
AMOCRM_BASE_URL = (os.getenv("AMOCRM_BASE_URL") or "").rstrip("/")
AMOCRM_CLIENT_ID = (os.getenv("AMOCRM_CLIENT_ID") or "").strip()
AMOCRM_CLIENT_SECRET = (os.getenv("AMOCRM_CLIENT_SECRET") or "").strip()
DEFAULT_AMOCRM_REDIRECT_PATH = "/amocrm/oauth/callback"
AMOCRM_REDIRECT_URI = (os.getenv("AMOCRM_REDIRECT_URI") or "").strip()
AMOCRM_ACCESS_TOKEN = (os.getenv("AMOCRM_ACCESS_TOKEN") or "").strip()
AMOCRM_REFRESH_TOKEN = (os.getenv("AMOCRM_REFRESH_TOKEN") or "").strip()
AMOCRM_PIPELINE_ID = (os.getenv("AMOCRM_PIPELINE_ID") or "").strip()
AMOCRM_STATUS_ID = (os.getenv("AMOCRM_STATUS_ID") or "").strip()
AMOCRM_RESPONSIBLE_USER_ID = (os.getenv("AMOCRM_RESPONSIBLE_USER_ID") or "").strip()
AMOCRM_TOKEN_FILE = os.getenv("AMOCRM_TOKEN_FILE") or os.path.join(os.path.dirname(__file__), "amocrm_token.json")

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

# ---------- AmoCRM auth & API ----------

def _load_amocrm_tokens_from_file() -> Dict[str, Any]:
    path = FsPath(AMOCRM_TOKEN_FILE) if AMOCRM_TOKEN_FILE else None
    if not path:
        return {}
    try:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        print(f"[amocrm] token file load error: {exc}")
    return {}

def _init_amocrm_token_state() -> Dict[str, Any]:
    stored = _load_amocrm_tokens_from_file()
    token_state: Dict[str, Any] = {
        "access_token": stored.get("access_token"),
        "refresh_token": stored.get("refresh_token"),
        "exp": 0,
    }
    expires_at = stored.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at > 0:
        token_state["exp"] = float(expires_at)

    if AMOCRM_ACCESS_TOKEN:
        token_state["access_token"] = AMOCRM_ACCESS_TOKEN
    if AMOCRM_REFRESH_TOKEN:
        token_state["refresh_token"] = AMOCRM_REFRESH_TOKEN

    return token_state

_amocrm_token: Dict[str, Any] = _init_amocrm_token_state()

def amocrm_resolve_redirect_uri() -> str:
    if AMOCRM_REDIRECT_URI:
        return AMOCRM_REDIRECT_URI
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}{DEFAULT_AMOCRM_REDIRECT_PATH}"
    return ""

def _amocrm_save_tokens() -> None:
    path = FsPath(AMOCRM_TOKEN_FILE) if AMOCRM_TOKEN_FILE else None
    if not path:
        return
    payload = {
        "access_token": _amocrm_token.get("access_token"),
        "refresh_token": _amocrm_token.get("refresh_token"),
        "expires_at": _amocrm_token.get("exp", 0) or 0,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[amocrm] token file save error: {exc}")

def _amocrm_update_tokens(*, access_token: Optional[str], refresh_token: Optional[str], expires_in: Optional[int] = None, expires_at: Optional[float] = None) -> Optional[str]:
    if access_token:
        _amocrm_token["access_token"] = access_token
    if refresh_token:
        _amocrm_token["refresh_token"] = refresh_token
    if expires_at is not None:
        _amocrm_token["exp"] = float(expires_at)
    elif expires_in is not None:
        try:
            _amocrm_token["exp"] = time.time() + int(expires_in)
        except Exception:
            _amocrm_token["exp"] = 0
    _amocrm_save_tokens()
    return _amocrm_token.get("access_token")

def amocrm_credentials_available() -> bool:
    return bool(AMOCRM_BASE_URL and AMOCRM_CLIENT_ID and AMOCRM_CLIENT_SECRET)

def amocrm_configured() -> bool:
    return bool(
        amocrm_credentials_available()
        and (_amocrm_token.get("refresh_token") or _amocrm_token.get("access_token"))
    )

def amocrm_build_authorization_url(state: str = "avito-bot") -> str:
    if not amocrm_credentials_available():
        raise RuntimeError("AmoCRM credentials are not fully configured")
    params = {
        "client_id": AMOCRM_CLIENT_ID,
        "state": state,
        "mode": "post_message",
    }
    redirect_uri = amocrm_resolve_redirect_uri()
    if redirect_uri:
        params["redirect_uri"] = redirect_uri
    return f"https://www.amocrm.ru/oauth?{urlencode(params)}"

def amocrm_exchange_authorization_code(code: str) -> Optional[Dict[str, Any]]:
    if not amocrm_credentials_available():
        raise RuntimeError("AmoCRM credentials are not fully configured")
    payload: Dict[str, Any] = {
        "client_id": AMOCRM_CLIENT_ID,
        "client_secret": AMOCRM_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
    }
    redirect_uri = amocrm_resolve_redirect_uri()
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    try:
        resp = requests.post(
            f"{AMOCRM_BASE_URL}/oauth2/access_token",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
    except Exception as exc:
        print("[amocrm] authorization_code exchange error:", exc)
        return None

    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in")
    expires_at = data.get("expires_at") if isinstance(data, dict) else None
    _amocrm_update_tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(expires_in) if expires_in is not None else None,
        expires_at=float(expires_at) if isinstance(expires_at, (int, float)) else None,
    )
    return data

def amocrm_refresh_access_token() -> Optional[str]:
    if not amocrm_configured() or not _amocrm_token["refresh_token"]:
        return None
    try:
        payload = {
            "grant_type": "refresh_token",
            "client_id": AMOCRM_CLIENT_ID,
            "client_secret": AMOCRM_CLIENT_SECRET,
            "refresh_token": _amocrm_token["refresh_token"],
        }
        redirect_uri = amocrm_resolve_redirect_uri()
        if redirect_uri:
            payload["redirect_uri"] = redirect_uri
        r = requests.post(
            f"{AMOCRM_BASE_URL}/oauth2/access_token",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json() if r.content else {}
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token") or _amocrm_token.get("refresh_token")
        expires_in = data.get("expires_in")
        expires_at = data.get("expires_at") if isinstance(data, dict) else None
        return _amocrm_update_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=int(expires_in) if expires_in is not None else None,
            expires_at=float(expires_at) if isinstance(expires_at, (int, float)) else None,
        )
    except Exception as e:
        print("[amocrm] token refresh error:", e)
        return None

def amocrm_access_token() -> Optional[str]:
    if not amocrm_configured():
        return None
    now = time.time()
    access_token = _amocrm_token.get("access_token")
    if access_token and _amocrm_token.get("exp", 0) - now > 60:
        return access_token
    if access_token and not _amocrm_token.get("refresh_token"):
        return access_token
    if access_token and not _amocrm_token.get("exp"):
        # токен задан вручную и нет информации об exp — используем как есть
        return access_token
    return amocrm_refresh_access_token()

def amocrm_headers() -> Optional[Dict[str, str]]:
    token = amocrm_access_token()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

CONTACT_PHONE_RE = re.compile(r"(?:(?:\+|8)\s*(?:\(\s*\d{3}\s*\)|\d{3})|\+?\d)[\d\s\-()]{5,}\d")
CONTACT_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

def extract_contacts(text: str) -> Dict[str, List[str]]:
    phones = []
    emails = []
    if not text:
        return {"phones": phones, "emails": emails}
    for match in CONTACT_PHONE_RE.finditer(text):
        cleaned = re.sub(r"[\s()-]", "", match.group())
        if cleaned not in phones:
            phones.append(cleaned)
    for match in CONTACT_EMAIL_RE.finditer(text):
        value = match.group().lower()
        if value not in emails:
            emails.append(value)
    return {"phones": phones, "emails": emails}

def amocrm_create_lead(chat_id: str, buyer_text: str, contacts: Dict[str, List[str]], item_id: Optional[Any] = None) -> None:
    if not amocrm_configured():
        return
    headers = amocrm_headers()
    if not headers:
        print("[amocrm] skip lead creation: no access token")
        return

    lead_name = f"Avito чат {chat_id}" if chat_id else "Авито чат"
    lead_payload: Dict[str, Any] = {"name": lead_name}
    if AMOCRM_PIPELINE_ID:
        try:
            lead_payload["pipeline_id"] = int(AMOCRM_PIPELINE_ID)
        except ValueError:
            lead_payload["pipeline_id"] = AMOCRM_PIPELINE_ID
    if AMOCRM_STATUS_ID:
        try:
            lead_payload["status_id"] = int(AMOCRM_STATUS_ID)
        except ValueError:
            lead_payload["status_id"] = AMOCRM_STATUS_ID
    if AMOCRM_RESPONSIBLE_USER_ID:
        try:
            lead_payload["responsible_user_id"] = int(AMOCRM_RESPONSIBLE_USER_ID)
        except ValueError:
            lead_payload["responsible_user_id"] = AMOCRM_RESPONSIBLE_USER_ID

    try:
        r = requests.post(
            f"{AMOCRM_BASE_URL}/api/v4/leads",
            headers=headers,
            json=[lead_payload],
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        print("[amocrm] create lead error:", e)
        return

    try:
        data = r.json()
        embedded = data.get("_embedded") if isinstance(data, dict) else None
        lead_list = embedded.get("leads") if isinstance(embedded, dict) else None
        lead_id = None
        if isinstance(lead_list, list) and lead_list:
            lead = lead_list[0]
            lead_id = lead.get("id")
    except Exception:
        lead_id = None

    note_lines = ["Контакты клиента из Авито чата:"]
    if contacts.get("phones"):
        note_lines.append("Телефоны: " + ", ".join(contacts["phones"]))
    if contacts.get("emails"):
        note_lines.append("Emails: " + ", ".join(contacts["emails"]))
    if item_id:
        note_lines.append(f"Объявление: https://avito.ru/{item_id}")
    note_lines.append("Фрагмент сообщения:")
    note_lines.append(buyer_text)
    note_text = "\n".join(note_lines)

    if lead_id is None:
        print("[amocrm] lead created but id unknown, skip note")
        return

    try:
        note_payload = [{"note_type": "common", "params": {"text": note_text[:4096]}}]
        resp = requests.post(
            f"{AMOCRM_BASE_URL}/api/v4/leads/{lead_id}/notes",
            headers=headers,
            json=note_payload,
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        print("[amocrm] add note error:", e)
        return

    print(f"[amocrm] lead created: {lead_id}")

# ---------- Avito API ----------

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

def avito_list_chats(account_id: str, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    url = f"{AVITO_BASE}/messenger/v2/accounts/{account_id}/chats"
    params = {"limit": max(1, min(limit, 100)), "offset": max(0, offset), "chat_types": "u2i"}
    r = requests.get(url, headers=avito_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def avito_list_messages(account_id: str, chat_id: str, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
    url = f"{AVITO_BASE}/messenger/v3/accounts/{account_id}/chats/{chat_id}/messages/"
    params = {"limit": max(1, min(limit, 100)), "offset": max(0, offset)}
    r = requests.get(url, headers=avito_headers(), params=params, timeout=20)
    r.raise_for_status()
    return r.json()

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

    # Убираем метки источников вида  【1:...】
    reply = re.sub(r"【\d+:[^】]+】", "", reply).strip()

    if REPLY_PREFIX:
        reply = f"{REPLY_PREFIX}{reply}"

    return reply[:1000]

# ---------- FastAPI ----------

db_init()
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(title="Avito AI Assistant Bot", root_path=ROOT_PATH or "")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Admin API ----------

from fastapi import UploadFile, File, Path as FastAPIPath, HTTPException
from fastapi import APIRouter

admin_api = APIRouter(prefix="/api/admin", tags=["admin"])

def build_avito_dialogs_txt(account_id: str) -> str:
    limit = 100

    def _chat_title(chat: Dict[str, Any]) -> str:
        title = chat.get("title")
        if title:
            return str(title)
        context = chat.get("context")
        if isinstance(context, dict):
            value = context.get("value")
            if isinstance(value, dict):
                for key in ("title", "name"):
                    if value.get(key):
                        return str(value[key])
            for key in ("title", "name"):
                if context.get(key):
                    return str(context[key])
        elif isinstance(context, str):
            return context
        return ""

    def _chat_item_url(chat: Dict[str, Any]) -> str:
        item_id = chat.get("item_id") or chat.get("itemId")
        if item_id:
            return f"https://avito.ru/{item_id}"
        context = chat.get("context")
        if isinstance(context, dict):
            value = context.get("value")
            if isinstance(value, dict):
                url = value.get("url")
                if url:
                    return str(url)
                item_id = value.get("id") or value.get("item_id")
                if item_id:
                    return f"https://avito.ru/{item_id}"
        return ""

    def _chat_participants(chat: Dict[str, Any]) -> List[str]:
        users = chat.get("users")
        parts: List[str] = []
        if isinstance(users, list):
            for u in users:
                if not isinstance(u, dict):
                    continue
                name = u.get("name") or u.get("user_name") or u.get("login")
                if not name and u.get("id") is not None:
                    name = f"user:{u['id']}"
                if name:
                    parts.append(str(name))
        return parts

    def _format_ts(value: Any) -> str:
        if value in (None, ""):
            return "—"
        try:
            return datetime.utcfromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(value)

    def _message_text(msg: Dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, dict):
            for key in ("text",):
                if isinstance(content.get(key), str):
                    return content[key]
            message = content.get("message")
            if isinstance(message, dict):
                for key in ("text", "body", "description"):
                    if isinstance(message.get(key), str):
                        return message[key]
            payload = content.get("payload")
            if isinstance(payload, dict):
                for key in ("text", "body"):
                    if isinstance(payload.get(key), str):
                        return payload[key]
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)

    chats: List[Dict[str, Any]] = []
    offset = 0
    while True:
        data = avito_list_chats(account_id, limit=limit, offset=offset)
        batch = data.get("chats") or data.get("result") or []
        if not isinstance(batch, list):
            batch = []
        chats.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    lines: List[str] = []
    lines.append(
        f"Avito dialogs dump (account {account_id}) — generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )
    lines.append(f"Всего чатов: {len(chats)}")
    lines.append("")

    for chat in chats:
        chat_id = chat.get("id") or chat.get("chat_id") or chat.get("chatId")
        if not chat_id:
            continue
        chat_id = str(chat_id)
        title = _chat_title(chat)
        item_url = _chat_item_url(chat)
        participants = _chat_participants(chat)

        lines.append("=" * 80)
        header = f"CHAT {chat_id}"
        if title:
            header += f" — {title}"
        lines.append(header)
        if item_url:
            lines.append(f"Товар: {item_url}")
        if participants:
            lines.append("Участники: " + ", ".join(participants))

        messages: List[Dict[str, Any]] = []
        offset = 0
        while True:
            data = avito_list_messages(account_id, chat_id, limit=limit, offset=offset)
            batch = data.get("messages") or data.get("result") or []
            if not isinstance(batch, list):
                batch = []
            messages.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        if not messages:
            lines.append("(сообщений нет)")
            lines.append("")
            continue

        def _msg_ts(msg: Dict[str, Any]) -> int:
            ts = msg.get("created") or msg.get("timestamp") or msg.get("created_at")
            try:
                return int(ts)
            except Exception:
                return 0

        messages.sort(key=_msg_ts)

        for msg in messages:
            ts = _format_ts(msg.get("created") or msg.get("timestamp") or msg.get("created_at"))
            author = msg.get("author_id") or msg.get("user_id") or msg.get("authorId")
            if author is None:
                author = "?"
            msg_type = msg.get("type") or msg.get("message_type") or "?"
            direction = msg.get("direction")
            mid = msg.get("id") or msg.get("message_id")
            prefix_parts = [f"[{ts}]", f"id={mid}" if mid else None, f"author={author}", f"type={msg_type}"]
            if direction:
                prefix_parts.append(f"direction={direction}")
            prefix = " ".join(part for part in prefix_parts if part)

            text = _message_text(msg)
            text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
            lines_text = text.split("\n") if text else [""]
            first_line = lines_text[0] if lines_text else ""
            lines.append(f"{prefix}: {first_line}" if first_line else (prefix + (":" if prefix else "")))
            for extra in lines_text[1:]:
                lines.append("    " + extra)

            attachments = msg.get("attachments")
            if attachments:
                try:
                    attachments_str = json.dumps(attachments, ensure_ascii=False)
                except Exception:
                    attachments_str = str(attachments)
                lines.append("    attachments: " + attachments_str)

        lines.append("")

    if not chats:
        lines.append("Чаты не найдены или недоступны.")

    return "\n".join(lines).strip() + "\n"

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
    return {
        "instructions": (a.instructions or ""),
        "assistant_id": getattr(a, "id", None),
        "vector_store_id": VECTOR_STORE_ID or None,
        "bot_enabled": BOT_ENABLED,
    }

@admin_api.put("/settings")
def admin_put_settings(payload: Dict[str, Any]):
    global BOT_ENABLED
    payload = payload or {}
    response: Dict[str, Any] = {"ok": True}

    if "bot_enabled" in payload:
        val = payload.get("bot_enabled")
        if isinstance(val, str):
            BOT_ENABLED = val.strip().lower() not in {"0", "false", "no", "off"}
        else:
            BOT_ENABLED = bool(val)
        response["bot_enabled"] = BOT_ENABLED

    if "instructions" in payload:
        instructions = payload.get("instructions") or ""
        aid = ensure_assistant_id()
        try:
            kwargs: Dict[str, Any] = {"instructions": instructions}
            if VECTOR_STORE_ID:
                kwargs["tools"] = [{"type": "file_search"}]
                kwargs["tool_resources"] = {"file_search": {"vector_store_ids": [VECTOR_STORE_ID]}}
            openai_client.beta.assistants.update(assistant_id=aid, **kwargs)
            response["instructions"] = instructions
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Assistant update failed: {e}")

    return response

# ===== ФАЙЛЫ (Vector Store или Files API) =====

@admin_api.get("/files")
def list_files():
    try:
        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            vs_list = openai_client.beta.vector_stores.files.list(vector_store_id=VECTOR_STORE_ID, limit=100)
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
                    "status": vs_status,
                    "last_error": last_error,
                })

            rows.sort(key=lambda x: (x.get("created_at") or 0), reverse=True)
            return {"data": rows}

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
                f = openai_client.files.create(file=(uf.filename, content), purpose="assistants")
                uploaded.append({"id": f.id, "filename": uf.filename, "target": "files"})

        return {"ok": True, "uploaded": uploaded}
    except Exception as e:
        print("[admin/files upload] error:", traceback.format_exc())
        return JSONResponse({"detail": f"Upload failed: {e}"}, status_code=500)

@admin_api.delete("/files/{file_id}", response_class=PlainTextResponse)
def delete_file(file_id: str = FastAPIPath(..., description="OpenAI File ID")):
    try:
        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            try:
                openai_client.beta.vector_stores.files.delete(vector_store_id=VECTOR_STORE_ID, file_id=file_id)
            except Exception:
                pass
        openai_client.files.delete(file_id)
        return "ok"
    except Exception as e:
        print("[admin/files delete] error:", traceback.format_exc())
        return PlainTextResponse(f"Delete failed: {e}", status_code=500)

@admin_api.get("/files/{file_id}")
def inspect_file(file_id: str):
    try:
        info: Dict[str, Any] = {"file_id": file_id}

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

        if VECTOR_STORE_ID and hasattr(openai_client.beta, "vector_stores"):
            try:
                vf = openai_client.beta.vector_stores.files.retrieve(vector_store_id=VECTOR_STORE_ID, file_id=file_id)
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

@admin_api.get("/dialogs.txt", response_class=PlainTextResponse)
def admin_download_dialogs_txt():
    if not AVITO_ACCOUNT_ID:
        raise HTTPException(status_code=400, detail="AVITO_ACCOUNT_ID is not configured")

    try:
        content = build_avito_dialogs_txt(AVITO_ACCOUNT_ID)
    except requests.HTTPError as e:
        status = e.response.status_code if getattr(e, "response", None) is not None else 502
        detail = e.response.text if getattr(e, "response", None) is not None else str(e)
        raise HTTPException(status_code=status, detail=f"Avito API error: {detail}")
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Avito request failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    filename = f"avito-dialogs-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.txt"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return PlainTextResponse(content, headers=headers)

# ---------- AmoCRM OAuth callback ----------

@app.get(DEFAULT_AMOCRM_REDIRECT_PATH, response_class=PlainTextResponse)
async def amocrm_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
):
    if error:
        message = f"OAuth error: {error}"
        if error_description:
            message += f" — {error_description}"
        return PlainTextResponse(message, status_code=400)

    if not code:
        return PlainTextResponse("Missing ?code parameter in callback", status_code=400)

    if not amocrm_credentials_available():
        return PlainTextResponse(
            "AmoCRM credentials are not configured. Check AMOCRM_BASE_URL/CLIENT_ID/CLIENT_SECRET.",
            status_code=500,
        )

    data = amocrm_exchange_authorization_code(code)
    if not data:
        return PlainTextResponse("Failed to exchange authorization code. See server logs for details.", status_code=502)

    saved_to = AMOCRM_TOKEN_FILE or ""
    message_lines = ["✅ AmoCRM tokens received."]
    if saved_to:
        message_lines.append(f"Tokens saved to: {saved_to}")
    if state:
        message_lines.append(f"state={state}")
    access_masked = data.get("access_token")
    refresh_masked = data.get("refresh_token")
    if isinstance(access_masked, str) and len(access_masked) > 8:
        access_masked = access_masked[:4] + "…" + access_masked[-4:]
    if isinstance(refresh_masked, str) and len(refresh_masked) > 8:
        refresh_masked = refresh_masked[:4] + "…" + refresh_masked[-4:]
    if access_masked:
        message_lines.append(f"access_token: {access_masked}")
    if refresh_masked:
        message_lines.append(f"refresh_token: {refresh_masked}")
    message_lines.append("You can close this tab and return to the bot.")
    return PlainTextResponse("\n".join(message_lines))

# Алиас: внешний /admin/... → тот же коллбек
@app.get("/admin/amocrm/oauth/callback")
async def amocrm_callback_alias(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    qs = []
    if code: qs.append(f"code={code}")
    if state: qs.append(f"state={state}")
    if error: qs.append(f"error={error}")
    if error_description: qs.append(f"error_description={error_description}")
    suffix = ("?" + "&".join(qs)) if qs else ""
    return RedirectResponse(url=DEFAULT_AMOCRM_REDIRECT_PATH + suffix)

# Роутер админки
app.include_router(admin_api)

@app.get("/health")
def health():
    return {"status": "ok", "root_path": ROOT_PATH or ""}

@app.post("/avito-webhook")
async def avito_webhook(request: Request, background: BackgroundTasks):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    if not BOT_ENABLED:
        return JSONResponse({"ok": True, "bot_enabled": False})

    # Лог входящего события
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
            user_id = msg.get("user_id")
            author_id = msg.get("author_id")
            msg_type = msg.get("type")
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

            contacts = extract_contacts(buyer_text)
            if contacts.get("phones") or contacts.get("emails"):
                amocrm_create_lead(chat_id, buyer_text, contacts, item_id=item_id)

            # контекст объявления (если есть)
            ctx = None
            if item_id:
                ctx = {"type": "item", "value": {"title": "", "price_string": "", "url": f"https://avito.ru/{item_id}"}}

            reply = run_assistant_and_get_reply(chat_id, buyer_text, ctx)

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

def cmd_amocrm_auth_url(state: str):
    try:
        url = amocrm_build_authorization_url(state=state)
    except Exception as exc:
        print(f"[amocrm] cannot build auth url: {exc}")
        return
    redirect_hint = amocrm_resolve_redirect_uri()
    if redirect_hint:
        print(f"[amocrm] redirect_uri: {redirect_hint}")
    else:
        print("[amocrm] WARNING: redirect_uri is empty. Set PUBLIC_BASE_URL or AMOCRM_REDIRECT_URI so AmoCRM "
              f"can call back {DEFAULT_AMOCRM_REDIRECT_PATH}")
    print("[amocrm] Open the following URL in a browser and authorize the integration:")
    print(url)

def cmd_amocrm_exchange_code(code: str):
    data = amocrm_exchange_authorization_code(code)
    if data is None:
        print("{}")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if AMOCRM_TOKEN_FILE:
        print(f"[amocrm] tokens saved to {AMOCRM_TOKEN_FILE}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true", help="Запустить HTTP-сервер (FastAPI/uvicorn)")
    parser.add_argument("--subscribe", metavar="URL", help="Подписать Avito webhook на URL")
    parser.add_argument("--whoami", action="store_true", help="Проверка /core/v1/accounts/self")
    parser.add_argument("--amocrm-auth-url", action="store_true", help="Вывести OAuth ссылку для получения authorization code")
    parser.add_argument("--amocrm-state", default="avito-bot", help="Значение параметра state для OAuth ссылки")
    parser.add_argument("--amocrm-exchange-code", metavar="CODE", help="Обмен authorization code AmoCRM на токены доступа")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.subscribe:
        cmd_subscribe(args.subscribe); return
    if args.whoami:
        cmd_whoami(); return
    if args.amocrm_auth_url:
        cmd_amocrm_auth_url(args.amocrm_state); return
    if args.amocrm_exchange_code:
        cmd_amocrm_exchange_code(args.amocrm_exchange_code); return
    if args.serve:
        import uvicorn
        uvicorn.run("avito_ai_assistant_bot:app",
                    host=args.host, port=args.port,
                    reload=False, proxy_headers=True, forwarded_allow_ips="*")
        return

    parser.print_help()

if __name__ == "__main__":
    main()
