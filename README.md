# 💬 Messenger

Полноценный мессенджер в реальном времени на FastAPI + WebSocket.

## 🚀 Быстрый запуск (локально)

```bash
# 1. Создать виртуальное окружение
python -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Запустить
uvicorn main:app --reload

# Открыть: http://localhost:8000
```

## 🌍 Деплой на Railway

1. **Создай репозиторий на GitHub** и запушь код:
   ```bash
   git init && git add . && git commit -m "init"
   git remote add origin https://github.com/ВАШ_ЮЗЕР/messenger.git
   git push -u origin main
   ```

2. **Открой [railway.app](https://railway.app)**
   - New Project → Deploy from GitHub repo
   - Выбери свой репозиторий

3. **Добавь переменные окружения** (Settings → Variables):
   ```
   SECRET_KEY = (любая случайная строка, минимум 32 символа)
   ```
   > Сгенерировать: `python3 -c "import secrets; print(secrets.token_hex(32))"`

4. **Добавь PostgreSQL** (опционально, но рекомендуется):
   - New → Database → PostgreSQL
   - `DATABASE_URL` добавится автоматически

5. **Готово!** Railway задеплоит сам. URL вида `https://xxx.railway.app`

## 📁 Структура

```
messenger/
├── main.py              # API + WebSocket роуты
├── models.py            # Таблицы БД
├── schemas.py           # Валидация данных
├── auth.py              # JWT + bcrypt
├── database.py          # Подключение SQLite/PostgreSQL
├── websocket_manager.py # Менеджер WS соединений
├── requirements.txt     # Зависимости (закреплены версии!)
├── nixpacks.toml        # Railway: Python 3.11
├── Procfile             # Railway: команда запуска
├── railway.json         # Railway: конфиг
└── static/
    └── index.html       # Весь фронтенд
```

## ✅ Функции

- Регистрация / вход (JWT, живёт 30 дней)
- Личные чаты (1 на 1)
- Групповые чаты с просмотром участников
- Сообщения в реальном времени (WebSocket)
- Онлайн/оффлайн статус
- Индикатор "печатает..."
- Галочки прочитанных ✓✓
- Счётчик непрочитанных
- Пагинация (загрузка старых сообщений)
- Защита от флуда (rate limiting)
- Экспоненциальный reconnect при обрыве связи
- Мобильная адаптация

## 🔌 WebSocket API

**Клиент → Сервер:**
```json
{"type": "message",  "room_id": 1, "content": "Привет!"}
{"type": "typing",   "room_id": 1, "is_typing": true}
{"type": "read",     "room_id": 1}
{"type": "ping"}
```

**Сервер → Клиент:**
```json
{"type": "new_message",  "message": {...}}
{"type": "user_status",  "user_id": 2, "is_online": true}
{"type": "typing",       "room_id": 1, "user_id": 2, "username": "Алиса", "is_typing": true}
{"type": "messages_read","room_id": 1, "reader_id": 2}
{"type": "error",        "message": "текст"}
{"type": "pong"}
```
