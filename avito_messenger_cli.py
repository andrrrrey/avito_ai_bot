#!/usr/bin/env python3
# avito_messenger_cli.py — серверный CLI для Avito Messenger
# Требования: pip install requests python-dotenv
# Примеры:
#   # окружение из .env:
#   set -a; source .env; set +a
#   # кто я
#   python3 avito_messenger_cli.py --whoami
#   # список чатов (u2i)
#   python3 avito_messenger_cli.py --list-chats --limit 20 --unread-only
#   # сообщения по чату
#   python3 avito_messenger_cli.py --messages CHAT_ID --limit 50
#   # отправить текст
#   python3 avito_messenger_cli.py --send CHAT_ID "привет!"
#   # пометить прочитанным
#   python3 avito_messenger_cli.py --read --chat-id CHAT_ID
#   # подписать вебхук
#   python3 avito_messenger_cli.py --subscribe-webhook https://dev.futuguru.com/Cash-Cross/avito-webhook
#   # дамп всех чатов/сообщений
#   python3 avito_messenger_cli.py --dump-all --limit 50
#   # «хвост»: печатай всё новое раз в N секунд
#   python3 avito_messenger_cli.py --watch --interval 10

import os
import sys
import time
import json
import argparse
from typing import Optional, Dict, Any, List
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()  # читает .env из cwd
except Exception:
    pass

BASE_URL = os.getenv("AVITO_BASE_URL", "https://api.avito.ru")
CLIENT_ID = os.getenv("AVITO_CLIENT_ID")
CLIENT_SECRET = os.getenv("AVITO_CLIENT_SECRET")

DEFAULT_TIMEOUT = int(os.getenv("AVITO_HTTP_TIMEOUT", "25"))

class AvitoAPIError(Exception):
    pass

def _is_json(r: requests.Response) -> bool:
    return r.headers.get("Content-Type","").startswith("application/json")

def _check_resp(r: requests.Response) -> Dict[str, Any]:
    if r.status_code >= 400:
        body = r.text
        raise AvitoAPIError(f"{r.request.method} {r.url} -> {r.status_code}: {body}")
    if r.content and _is_json(r):
        return r.json()
    return {}

def get_token_client_credentials(client_id: str, client_secret: str) -> Dict[str, Any]:
    if not client_id or not client_secret:
        raise AvitoAPIError("AVITO_CLIENT_ID / AVITO_CLIENT_SECRET не заданы (см. .env).")
    r = requests.post(
        f"{BASE_URL}/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    data = _check_resp(r)
    if "access_token" not in data:
        raise AvitoAPIError(f"Нет access_token в ответе: {data}")
    return data

class Avito:
    def __init__(self, client_id: str, client_secret: str, verbose: bool=False):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: Optional[str] = None
        self.exp_at: float = 0
        self.verbose = verbose

    def _ensure_token(self):
        if self.token and time.time() < self.exp_at - 10:
            return
        t = get_token_client_credentials(self.client_id, self.client_secret)
        self.token = t["access_token"]
        self.exp_at = time.time() + int(t.get("expires_in", 3600))
        if self.verbose:
            print(f"[auth] token ok, expires_in={int(t.get('expires_in', 3600))}s")

    def _headers(self) -> Dict[str, str]:
        self._ensure_token()
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    # ---- User
    def get_self(self) -> Dict[str, Any]:
        r = requests.get(f"{BASE_URL}/core/v1/accounts/self", headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        return _check_resp(r)

    # ---- Messenger v2: список чатов
    def list_chats_v2(
        self,
        user_id: int,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
        chat_types: str = "u2i",
        item_ids: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = {
            "limit": max(1, min(limit, 100)),
            "offset": max(0, min(offset, 10000)),
            "unread_only": str(unread_only).lower(),
            "chat_types": chat_types,
        }
        if item_ids:
            params["item_ids"] = item_ids
        r = requests.get(
            f"{BASE_URL}/messenger/v2/accounts/{user_id}/chats",
            headers=self._headers(),
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        return _check_resp(r)

    # ---- Messenger v3: список сообщений
    def list_messages_v3(self, user_id: int, chat_id: str, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        params = {"limit": max(1, min(limit, 100)), "offset": max(0, min(offset, 10000))}
        r = requests.get(
            f"{BASE_URL}/messenger/v3/accounts/{user_id}/chats/{chat_id}/messages/",
            headers=self._headers(),
            params=params,
            timeout=DEFAULT_TIMEOUT,
        )
        return _check_resp(r)

    # ---- Messenger v1: отправка текста
    def send_text_v1(self, user_id: int, chat_id: str, text: str) -> Dict[str, Any]:
        body = {"type": "text", "message": {"text": text}}
        r = requests.post(
            f"{BASE_URL}/messenger/v1/accounts/{user_id}/chats/{chat_id}/messages",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        return _check_resp(r)

    # ---- Messenger v1: отметка прочитанным
    def mark_read_v1(self, user_id: int, chat_id: str) -> Dict[str, Any]:
        r = requests.post(
            f"{BASE_URL}/messenger/v1/accounts/{user_id}/chats/{chat_id}/read",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        return _check_resp(r)

    # ---- Messenger v3: подписка вебхука
    def subscribe_webhook_v3(self, url: str) -> Dict[str, Any]:
        body = {"url": url}
        r = requests.post(
            f"{BASE_URL}/messenger/v3/webhook",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        return _check_resp(r)

def pretty(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))

def print_messages_dump(messages: List[Dict[str, Any]]):
    for m in messages:
        mid = m.get("id")
        created = m.get("created") or m.get("timestamp")
        try:
            tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(created)))
        except Exception:
            tstr = str(created)
        author_id = m.get("author_id")
        mtype = m.get("type")
        content = m.get("content") or {}
        text = None
        if isinstance(content, dict):
            text = content.get("text") or content.get("message",{}).get("text")
        if text is None:
            text = json.dumps(content, ensure_ascii=False)
        print(f"[{tstr}] chat_msg_id={mid} author={author_id} type={mtype} text={text}")

def main():
    ap = argparse.ArgumentParser(description="Avito Messenger CLI (server)")
    ap.add_argument("--client-id", default=CLIENT_ID)
    ap.add_argument("--client-secret", default=CLIENT_SECRET)
    ap.add_argument("--account-id", type=int, help="user_id. Если не задан — возьмём из /core/v1/accounts/self")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--unread-only", action="store_true")
    ap.add_argument("--chat-types", default="u2i")
    ap.add_argument("--item-ids")
    ap.add_argument("--whoami", action="store_true")
    ap.add_argument("--list-chats", action="store_true")
    ap.add_argument("--messages", help="chat_id для выдачи сообщений")
    ap.add_argument("--chat-id", help="chat_id (для --messages/--send/--read)")
    ap.add_argument("--send", nargs="+", help="Отправить текст. Пример: --send CHAT_ID 'привет!' или --chat-id CHAT --send 'текст'")
    ap.add_argument("--read", action="store_true", help="Пометить чат прочитанным (требуется --chat-id)")
    ap.add_argument("--subscribe-webhook", help="URL вебхука")
    ap.add_argument("--dump-all", action="store_true", help="Вывести по всем чатам последние сообщения (limit)")
    ap.add_argument("--watch", action="store_true", help="Печатать новые непрочитанные сообщения (poll)")
    ap.add_argument("--interval", type=int, default=10, help="интервал опроса при --watch, сек")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    api = Avito(args.client_id, args.client_secret, verbose=args.verbose)

    # user_id
    user_id = args.account_id
    me = api.get_self()
    if args.whoami:
        print("== /core/v1/accounts/self ==")
        pretty(me)
    if not user_id:
        user_id = me.get("id")
        if not user_id:
            raise AvitoAPIError("Не удалось определить user_id из /core/v1/accounts/self. Укажи --account-id вручную.")

    # list chats
    if args.list_chats:
        data = api.list_chats_v2(
            user_id=user_id,
            limit=args.limit,
            offset=args.offset,
            unread_only=args.unread_only,
            chat_types=args.chat_types,
            item_ids=args.item_ids,
        )
        print("== chats (v2) ==")
        pretty(data)

    # messages
    if args.messages or (args.chat_id and not args.send and not args.read):
        chat_id = args.messages or args.chat_id
        data = api.list_messages_v3(user_id=user_id, chat_id=chat_id, limit=args.limit, offset=args.offset)
        print(f"== messages (v3) chat_id={chat_id} ==")
        items = data.get("messages") or data.get("result") or []
        print_messages_dump(items)

    # send text
    if args.send:
        if len(args.send) == 1 and args.chat_id:
            chat_id = args.chat_id
            text = args.send[0]
        else:
            if len(args.send) < 2:
                raise AvitoAPIError("Использование: --send CHAT_ID 'текст' (или --chat-id CHAT_ID --send 'текст')")
            chat_id, text = args.send[0], " ".join(args.send[1:])
        data = api.send_text_v1(user_id=user_id, chat_id=chat_id, text=text)
        print(f"== sent (v1) chat_id={chat_id} ==")
        pretty(data)

    # mark read
    if args.read:
        if not args.chat_id:
            raise AvitoAPIError("--read требует --chat-id")
        data = api.mark_read_v1(user_id=user_id, chat_id=args.chat_id)
        print(f"== read OK (v1) chat_id={args.chat_id} ==")
        pretty(data)

    # subscribe webhook
    if args.subscribe_webhook:
        data = api.subscribe_webhook_v3(args.subscribe_webhook)
        print("== webhook subscribe (v3) ==")
        pretty(data)

    # dump all chats/messages
    if args.dump_all:
        data = api.list_chats_v2(
            user_id=user_id,
            limit=args.limit,
            offset=args.offset,
            unread_only=False,
            chat_types=args.chat_types,
            item_ids=args.item_ids,
        )
        chats = data.get("chats") or data.get("result") or []
        print(f"== total chats: {len(chats)} ==")
        for ch in chats:
            cid = ch.get("id") or ch.get("chat_id")
            title = ch.get("title") or ch.get("context")
            print("="*88)
            print(f"CHAT {cid} | {title}")
            msgs = api.list_messages_v3(user_id=user_id, chat_id=cid, limit=args.limit, offset=args.offset)
            items = msgs.get("messages") or msgs.get("result") or []
            print_messages_dump(list(reversed(items)))  # старые -> новые

    # watch (poll unread only)
    if args.watch:
        seen: set[str] = set()
        print(f"== watching unread every {args.interval}s… Ctrl+C to stop")
        while True:
            try:
                data = api.list_chats_v2(
                    user_id=user_id,
                    limit=100,
                    offset=0,
                    unread_only=True,
                    chat_types=args.chat_types,
                    item_ids=args.item_ids,
                )
                chats = data.get("chats") or data.get("result") or []
                for ch in chats:
                    cid = ch.get("id") or ch.get("chat_id")
                    msgs = api.list_messages_v3(user_id=user_id, chat_id=cid, limit=50, offset=0)
                    items = msgs.get("messages") or msgs.get("result") or []
                    # печатаем только новые id
                    for m in items:
                        mid = m.get("id")
                        if mid and mid not in seen:
                            print_messages_dump([m])
                            seen.add(mid)
                time.sleep(max(1, args.interval))
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"[watch] error: {e}")
                time.sleep(max(1, args.interval))

if __name__ == "__main__":
    try:
        main()
    except AvitoAPIError as e:
        print("API error:", e)
        sys.exit(2)
    except requests.RequestException as e:
        print("Network error:", e)
        sys.exit(3)
