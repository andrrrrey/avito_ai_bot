# avito_ai_bot

## 🤖 Avito AI Assistant Bot

**Автоматический ИИ-ассистент для продавца на Авито**

Работает через **FastAPI + OpenAI Assistants API (gpt-4o-mini)**
и подключён к **Avito Messenger Webhook**.
Поддерживает загрузку файлов в Vector Store, админ-панель
и гибкую конфигурацию через `.env`.

---

## 🚀 Возможности

* Принимает сообщения покупателей из **Avito Messenger (webhook)**
* Отвечает от имени продавца с помощью **OpenAI Assistants**
* Сохраняет контекст диалогов в **SQLite (`threads.sqlite3`)**
* Поддерживает **Vector Store (file search)** для инструкций и документов
* Имеет **админ-панель** для редактирования инструкций и загрузки файлов
* Быстрая смена ключей, ассистента и вебхука через `.env`

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
```

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

> ⚠️ Не выполняй `source .env` напрямую в bash — значения с пробелами и скобками не поддерживаются.
> Файл читается автоматически через `python-dotenv`.

---

### 3. Тестовый запуск

```bash
source .venv/bin/activate
python3 avito_ai_assistant_bot.py --serve --host 127.0.0.1 --port 8081
```

Админка будет доступна по адресу:

* `https://dev.futuguru.com/Cash-Cross/api/admin/settings`
* `https://dev.futuguru.com/Cash-Cross/api/admin/files`

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

## 🧠 Админка API

| Endpoint                         | Описание                              |
| -------------------------------- | ------------------------------------- |
| **GET /api/admin/settings**      | Получить инструкции ассистента        |
| **PUT /api/admin/settings**      | Обновить инструкции напрямую в OpenAI |
| **GET /api/admin/files**         | Список файлов из Vector Store         |
| **POST /api/admin/files**        | Загрузить файлы                       |
| **DELETE /api/admin/files/{id}** | Удалить файл                          |
| **GET /api/admin/files/{id}**    | Проверить статус файла                |

---

## 🔐 Интеграция с AmoCRM

### 1. Создай интеграцию в AmoCRM

`Настройки → Интеграции → Управление интеграциями → Добавить интеграцию`
Запиши `client_id` и `client_secret`.
В поле **URL перенаправления** укажи:

```
https://<твой-домен>/admin/amocrm/oauth/callback
```

---

### 2. Заполни `.env`

```ini
AMOCRM_BASE_URL=https://karlomaster.amocrm.ru
AMOCRM_CLIENT_ID=abcd73f3-f7a5-4b7d-9cc5-54a395911114
AMOCRM_CLIENT_SECRET=hdgZD8QfK0B9iCCJCVrwinMBapd2fyPwZ3grashuced0Zye13FEqJ1YjSWhNxRDx
AMOCRM_REDIRECT_URI=https://novikov.futuguru.com/admin/amocrm/oauth/callback
AMOCRM_TOKEN_FILE=/home/bots/avito_cash_cross/amocrm_token.json
```

Если домен отличается — укажи полный `AMOCRM_REDIRECT_URI`.
`PUBLIC_BASE_URL` не обязателен, если домен совпадает с Avito webhook.

---

### 3. Получи новую ссылку авторизации

```bash
python avito_ai_assistant_bot.py --amocrm-auth-url
```

В консоли появится ссылка, например:

```
https://www.amocrm.ru/oauth?client_id=abcd...&redirect_uri=https%3A%2F%2Fnovikov.futuguru.com%2Fadmin%2Famocrm%2Foauth%2Fcallback
```

Открой её в браузере, авторизуй интеграцию и разреши доступ.

---

### 4. Заверши обмен токенов

**Если callback-страница открылась** — бот сам сохранит `access_token` и `refresh_token`
в файл `/home/bots/avito_cash_cross/amocrm_token.json`.

**Если видишь 404 или redirect не сработал**, скопируй параметр `code` из адресной строки:

```
?code=def50200...&state=avito-bot
```

и выполни:

```bash
python avito_ai_assistant_bot.py --amocrm-exchange-code "def50200..."
```

В ответе появятся новые токены:

```json
{
  "access_token": "eyJ0eXAiOi...",
  "refresh_token": "def502009c1ad...",
  "expires_in": 86400
}
```

---

### 5. Обнови `.env`

```ini
AMOCRM_ACCESS_TOKEN=
AMOCRM_REFRESH_TOKEN=def502009c1ad...   # вставь новый токен целиком
```

Перезапусти сервис:

```bash
sudo systemctl restart avito-bot
sudo journalctl -u avito-bot -f
```

После первого обращения к AmoCRM бот автоматически создаст файл
`amocrm_token.json` и дальше будет сам обновлять токены раз в сутки.

---

### ⚠️ Важно

* Каждый `refresh_token` **одноразовый**. После любого ручного `curl` или `--amocrm-exchange-code` предыдущий токен становится недействительным.
* После успешного обмена **новый refresh_token нужно вписать в `.env`**.
* Если бот работает корректно — **больше ничего вручную обновлять не нужно.**

---

## 🔁 Переподписка вебхука Avito

Если поменял `AVITO_CLIENT_ID` / `AVITO_CLIENT_SECRET` / `AVITO_USER_ID`,
обязательно переподпиши webhook:

```bash
export $(grep -E '^(AVITO_CLIENT_ID|AVITO_CLIENT_SECRET|AVITO_USER_ID)=' .env | xargs)

AVITO_TOKEN=$(curl -sS -X POST "https://api.avito.ru/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "grant_type=client_credentials&client_id=${AVITO_CLIENT_ID}&client_secret=${AVITO_CLIENT_SECRET}" \
  | jq -r '.access_token')

python3 avito_ai_assistant_bot.py --subscribe "https://dev.futuguru.com/Cash-Cross/avito-webhook"
sudo systemctl restart avito-bot
```

---

## 🧹 Очистка старых thread’ов

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

* Все ключи редактируются **только в `.env`**, потом:

  ```bash
  sudo systemctl restart avito-bot
  ```
* Ошибка “No thread found…” → очисти `threads.sqlite3`.
* Не делай `curl`-refresh, если бот уже сам обновляет токены.
* Для тестов webhook используй одинарные кавычки `'...'`, чтобы bash не ломал `!`.

---

## ✅ Проверка полного цикла

1. Отправь сообщение продавцу на Авито.
2. В логах VPS появится:

   ```
   [webhook] chat=u2i-xxxx type=text text=Привет
   [reply] -> chat=... ok
   ```
3. Через 1–3 секунды бот ответит в чате Авито.
4. При первом обмене с AmoCRM появится файл `amocrm_token.json`.

---

## 👨‍💻 Автор

**Andrei Pokrovskii**
Founder of [Moverlab.ru](https://moverlab.ru) • Creator of [futu.one](https://futu.one)
Digital entrepreneur, AI-developer, product architect.

---

**Бэкенд:** Python 3.12 + FastAPI + OpenAI SDK
**Развёртывание:** systemd + nginx reverse proxy
**Версия:** production 2025-11-10
