#!/usr/bin/env python3
import os
import sys
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()  # подхватит .env из текущей директории

BASE = "https://api.avito.ru"
TIMEOUT = 30

AVITO_CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
AVITO_CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")
# Можно задавать либо AVITO_ACCOUNT_ID, либо AVITO_USER_ID — возьмём что есть
AVITO_ACCOUNT_ID = os.getenv("AVITO_ACCOUNT_ID") or os.getenv("AVITO_USER_ID")

def need(name, val):
    if not val:
        print(f"ENV error: missing {name} in .env")
        sys.exit(2)

def get_token_client_credentials():
    r = requests.post(
        f"{BASE}/token",
        data={"grant_type": "client_credentials"},
        auth=(AVITO_CLIENT_ID, AVITO_CLIENT_SECRET),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()

def whoami(token):
    r = requests.get(
        f"{BASE}/core/v1/accounts/self",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    return r

def list_chats(token, account_id, limit=50):
    url = f"{BASE}/messenger/v1/accounts/{account_id}/chats"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params={"limit": limit}, timeout=TIMEOUT)
    return r

def list_messages(token, account_id, chat_id, limit=50):
    url = f"{BASE}/messenger/v1/accounts/{account_id}/chats/{chat_id}/messages"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params={"limit": limit}, timeout=TIMEOUT)
    return r

def fmt_ts(ts):
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

def main():
    need("AVITO_CLIENT_ID", AVITO_CLIENT_ID)
    need("AVITO_CLIENT_SECRET", AVITO_CLIENT_SECRET)
    need("AVITO_ACCOUNT_ID (или AVITO_USER_ID)", AVITO_ACCOUNT_ID)

    t = get_token_client_credentials()
    access_token = t.get("access_token")
    token_type = t.get("token_type")
    scope = t.get("scope")

    print(f"OK: got token. type={token_type} scope={scope} account_id={AVITO_ACCOUNT_ID}")

    # Проверим, кого мы видим этим токеном
    r_me = whoami(access_token)
    if r_me.status_code != 200:
        print(f"[core/self] HTTP {r_me.status_code}: {r_me.text}")
    else:
        print(f"[core/self] {r_me.json()}")

    # Пробуем Messenger
    r_ch = list_chats(access_token, AVITO_ACCOUNT_ID, limit=50)
    if r_ch.status_code != 200:
        print(f"[messenger/chats] HTTP {r_ch.status_code}: {r_ch.text}")
        print("\nДиагностика:")
        if r_ch.status_code in (401, 403, 404):
            print("- Частая причина: для Messenger нужен пользовательский токен (authorization_code), а не client_credentials.")
            print("- Проверь права приложения в кабинете Авито: доступ к Messenger (чтение/запись).")
            print("- Убедись, что AVITO_ACCOUNT_ID — это именно ID аккаунта, для которого включён Messenger.")
        sys.exit(1)

    data = r_ch.json()
    chats = data.get("chats") or data.get("result") or []
    print(f"Found chats: {len(chats)}")

    for ch in chats:
        chat_id = ch.get("id") or ch.get("chat_id")
        title = ch.get("title") or ch.get("context") or ""
        print("="*90)
        print(f"CHAT {chat_id}  {title}")

        r_msgs = list_messages(access_token, AVITO_ACCOUNT_ID, chat_id, limit=50)
        if r_msgs.status_code != 200:
            print(f"[messages] chat={chat_id} HTTP {r_msgs.status_code}: {r_msgs.text}")
            continue

        msgs = r_msgs.json().get("messages") or r_msgs.json().get("result") or []
        for m in reversed(msgs):
            mid = m.get("id")
            created = m.get("created") or m.get("timestamp")
            author_id = m.get("author_id")
            mtype = m.get("type")
            content = m.get("content")
            text = content.get("text") if isinstance(content, dict) else content
            print(f"[{fmt_ts(created)}] {author_id} ({mtype}): {text or json.dumps(content, ensure_ascii=False)}")

    print("="*90)
    print("Done.")

if __name__ == "__main__":
    main()
