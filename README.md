# avito_ai_bot

# 🤖 Avito AI Assistant Bot

**Автоматический ИИ-ассистент для продавца на Авито**  
Работает через **FastAPI + OpenAI Assistants API (gpt-4o-mini)** и подключён к **Avito Messenger Webhook**.  
Поддерживает загрузку файлов в Vector Store, админ-панель и гибкую конфигурацию через `.env`.

---

## 🚀 Возможности

- Принимает сообщения покупателей из Avito Messenger (через webhook)
- Отвечает от имени продавца с помощью **OpenAI Assistants**
- Сохраняет контекст диалогов в **SQLite (`threads.sqlite3`)**
- Поддерживает **Vector Store** (file search) для инструкций и документов
- Имеет **админ-панель** для редактирования инструкций и загрузки файлов
- Быстрая смена ключей, ассистента и вебхука через `.env`

---

## 📦 Установка

### 1. Подготовка окружения

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git jq
cd /home/bots
git clone https://github.com/andrrrrey/avito_cash_cross.git
cd avito_cash_cross
python3 -m venv .venv
source .venv/bin/activate
pip install -U fastapi uvicorn requests python-dotenv "openai==1.*" python-multipart
````

---

### 2. Конфигурация `.env`

Создай `/home/bots/avito_cash_cross/.env` со своими параметрами:

```ini
# --- OpenAI ---
OPENAI_API_KEY=sk-...
OPENAI_ASSISTANT_ID=asst_...
VECTOR_STORE_ID=vs_...

# --- Avito OAuth2 app ---
AVITO_CLIENT_ID=...
AVITO_CLIENT_SECRET=...
AVITO_USER_ID=...           # id из /core/v1/accounts/self

# --- Профиль продавца ---
SELLER_PROFILE_NAME=Cash-Cross
SELLER_PROFILE_ABOUT=Ремонт автоэлектрики, выездная диагностика.
SELLER_PROFILE_RULES=Обращайтесь вежливо, указывайте модель авто и год.
SELLER_PROFILE_FAQ=Работаем ежедневно 9:00–21:00.

# --- Прочее ---
ROOT_PATH=/Cash-Cross
PORT=8081
REPLY_PREFIX=[Авито]
```

> ⚠️ Не source’и `.env` напрямую в bash — значения с пробелами и скобками не поддерживаются.
> Файл читается через `python-dotenv`.

---

### 3. Тестовый запуск

```bash
source .venv/bin/activate
python3 avito_ai_assistant_bot.py --serve --host 127.0.0.1 --port 8081
```

Админка будет доступна по адресу:
`https://dev.futuguru.com/Cash-Cross/api/admin/settings`
или
`https://dev.futuguru.com/Cash-Cross/api/admin/files`

---

## ⚙️ Установка как сервис (systemd)

Создай `/etc/systemd/system/avito-bot.service`:

```ini
[Unit]
Description=Avito AI Assistant Bot (FastAPI + Uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/bots/avito_cash_cross
EnvironmentFile=/home/bots/avito_cash_cross/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/bots/avito_cash_cross/.venv/bin/uvicorn avito_ai_assistant_bot:app \
  --host 127.0.0.1 \
  --port 8081 \
  --proxy-headers \
  --forwarded-allow-ips="*" \
  --log-level info
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable avito-bot
sudo systemctl start avito-bot
sudo journalctl -u avito-bot -f --no-pager
```

---

## 🧠 Админка

| Endpoint                           | Описание                                    |
| ---------------------------------- | ------------------------------------------- |
| **GET `/api/admin/settings`**      | Получить инструкции ассистента              |
| **PUT `/api/admin/settings`**      | Обновить инструкции напрямую в OpenAI       |
| **GET `/api/admin/files`**         | Список файлов из Vector Store или Files API |
| **POST `/api/admin/files`**        | Загрузить файлы                             |
| **DELETE `/api/admin/files/{id}`** | Удалить файл                                |
| **GET `/api/admin/files/{id}`**    | Проверить статус файла                      |

---

## 🔁 Переподписка вебхука (новый аккаунт Avito)

Если поменял `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` / `AVITO_USER_ID`,
обязательно **переподпиши webhook**.

### 1. Получи токен

```bash
export $(grep -E '^(AVITO_CLIENT_ID|AVITO_CLIENT_SECRET|AVITO_USER_ID)=' .env | xargs)

AVITO_TOKEN=$(curl -sS -X POST "https://api.avito.ru/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "grant_type=client_credentials&client_id=${AVITO_CLIENT_ID}&client_secret=${AVITO_CLIENT_SECRET}" \
  | jq -r '.access_token')

curl -sS "https://api.avito.ru/core/v1/accounts/self" \
  -H "Authorization: Bearer $AVITO_TOKEN" | jq
```

### 2. Подпиши webhook

```bash
python3 avito_ai_assistant_bot.py --subscribe "https://dev.futuguru.com/Cash-Cross/avito-webhook"
```

### 3. Перезапусти сервис

```bash
sudo systemctl restart avito-bot
sudo journalctl -u avito-bot -f --no-pager
```

---

## 🧹 Очистка старых thread’ов и ассистента

Если менялся `OPENAI_API_KEY`, `OPENAI_ASSISTANT_ID` или `VECTOR_STORE_ID`:

```bash
cd /home/bots/avito_cash_cross
python3 - <<'PY'
import sqlite3, os
db="threads.sqlite3"
if os.path.exists(db):
    conn=sqlite3.connect(db)
    conn.execute("DELETE FROM threads;"); conn.commit(); conn.close()
    print("OK: threads очищены")
else:
    print("Нет threads.sqlite3")
PY

rm -f assistant_id.txt
sudo systemctl restart avito-bot
```

---

## 🪵 Отладка

Проверить переменные окружения:

```bash
systemctl show avito-bot --property=Environment | tr ' ' '\n' | egrep 'AVITO_|OPENAI|VECTOR'
```

Проверить webhook:

```bash
curl -i "https://dev.futuguru.com/Cash-Cross/avito-webhook" -H 'Content-Type: application/json' -d '{}'
```

Проверить чаты:

```bash
AVITO_TOKEN=$(curl -sS -X POST "https://api.avito.ru/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "grant_type=client_credentials&client_id=${AVITO_CLIENT_ID}&client_secret=${AVITO_CLIENT_SECRET}" \
  | jq -r '.access_token')

curl -sS "https://api.avito.ru/messenger/v1/accounts/${AVITO_USER_ID}/chats?limit=50" \
  -H "Authorization: Bearer $AVITO_TOKEN" | jq .
```

---

## 💡 Советы

* Все ключи (`OPENAI_API_KEY`, `OPENAI_ASSISTANT_ID`, `VECTOR_STORE_ID`)
  редактируются **только в `.env`** → затем

  ```bash
  sudo systemctl restart avito-bot
  ```
* Ошибка `"No thread found..."` → очисти `threads.sqlite3`.
* Для тестов webhook используй одинарные кавычки `'...'`,
  чтобы bash не ломал `!` внутри текста.

---

## ✅ Проверка полного цикла

1. Отправь сообщение продавцу на Авито.
2. В логах VPS появится:

   ```
   [webhook] chat=u2i-xxxx type=text text=Привет
   [reply] -> chat=... ok
   ```
3. Через 1–3 секунды бот ответит в чате Авито.

---

## 👨‍💻 Автор

**Andrei Pokrovskii**
Founder of [Moverlab.ru](https://moverlab.ru) • Creator of [futu.one](https://futu.one)
Digital entrepreneur, AI-developer, product architect.

---

**Бэкенд:** Python 3.12 + FastAPI + OpenAI SDK
**Развёртывание:** systemd + nginx reverse proxy
**Версия:** production 2025-10-15
