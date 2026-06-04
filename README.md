# AIGate Video Bot + Telegram Mini App

Проект для генерации видео через AIGate внутри Telegram:

- Telegram-бот на `aiogram 3`
- backend API на `FastAPI`
- Mini App на `React + Vite`
- SQLite для пользователей и истории генераций
- AIGate API: `https://api.aigate.shop/v1`

Пользователь регистрируется по вашей ссылке, пополняет свой баланс AIGate, вводит свой API-ключ в боте или Mini App, а генерации списываются с его баланса.

## Что умеет

- Подключение API-ключа AIGate
- Проверка баланса
- Два понятных режима: `Fast` и `Standard`
- Генерация через `/v1/video/generations`
- Фото-референсы до 6 штук
- 1 видео-референс через `input_video_b64`
- 1 аудио-референс экспериментально через `input_audio_b64` / `provider_options`
- Промпт, negative prompt, seed
- Длительность 4-15 секунд, разрешение, формат, звук
- Проверка баланса прямо в Mini App
- WebSocket-статус генерации
- Галерея результатов
- Скачивание временной ссылки AIGate в локальное `media/`

## Fast и Standard

В интерфейсе показываются только 2 режима:

- `Fast`
- `Standard`

Конкретные model id задаются в backend `.env`:

```env
FAST_MODEL_ID=bytedance/seedance-2.0-fast
STANDARD_MODEL_ID=bytedance/seedance-2.0
```

Списание денег всё равно выполняет AIGate по API-ключу пользователя. Цены в UI считаются по разрешению и секундам:

```env
FAST_PRICE_480P=0.01614
FAST_PRICE_720P=0.0363
FAST_PRICE_1080P=0.08166
STANDARD_PRICE_480P=0.020178
STANDARD_PRICE_720P=0.04536
STANDARD_PRICE_1080P=0.10206
```

`Fast` и `Standard` не отправляются в поле `quality`; это разные `model id`.

## Важный момент про референсы

В AIGate для видео официально описан один image-reference:

- `input_image_b64`
- `input_image`
- `input_image_url`
- `image_url`
- `input_video_b64` для video-to-video, если модель поддерживает

В этом проекте можно загрузить до 6 фото:

- первое фото отправляется официально как `input_image_b64`
- остальные фото отправляются экспериментально в `provider_options.input_images`
- видео отправляется как `input_video_b64`
- аудио-референс отправляется экспериментально как `input_audio_b64` и `provider_options.audio_reference_b64`
- если AIGate вернёт `400` из-за экспериментальных референсов, backend повторит генерацию с официальным набором

Так пользователь получает удобный интерфейс, а генерация не ломается, если конкретная модель не принимает несколько референсов.

## Структура

```text
aigate_mini_app/
  backend/
    app/
      api_client.py    AIGate-клиент
      api_routes.py    API для Mini App
      database.py      SQLite
      handlers.py      Telegram-бот
      keyboards.py     Кнопки бота
      websocket.py     Прогресс генерации
    main.py            bot + FastAPI
    Dockerfile
    railway.toml
  frontend/
    src/
      App.jsx          интерфейс Mini App
      api.js           HTTP + WebSocket клиент
      index.css        дизайн
    vercel.json
```

## Backend: локальный запуск

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Для Windows PowerShell:

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

Минимальный `.env`:

```env
BOT_TOKEN=123456:telegram_bot_token
WEBAPP_URL=https://your-frontend.vercel.app
REFERRAL_URL=https://aigate.shop/?ref=your_ref
CORS_ORIGINS=https://your-frontend.vercel.app
FAST_MODEL_ID=bytedance/seedance-2.0-fast
STANDARD_MODEL_ID=bytedance/seedance-2.0
```

## Frontend: локальный запуск

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

`.env` фронта:

```env
VITE_API_URL=https://your-backend.railway.app
```

## Railway deploy без Vercel

Проект можно запускать одним сервисом на Railway:

- root directory: корень репозитория
- Dockerfile: `Dockerfile` в корне
- frontend собирается внутри Docker
- backend отдаёт API, Telegram-бота и готовый frontend с одного домена

Переменные Railway:

```env
BOT_TOKEN=...
WEBAPP_URL=https://your-app.up.railway.app
REFERRAL_URL=https://aigate.shop/?ref=your_ref
CORS_ORIGINS=https://your-app.up.railway.app
ALLOW_DEV_AUTH=false
```

`WEBAPP_URL` нужно заменить на публичный домен Railway после первого deploy. Потом этот же URL ставится в BotFather как Web App URL.

## BotFather

1. Откройте `@BotFather`.
2. `Bot Settings` -> `Menu Button`.
3. Выберите `Configure menu button`.
4. Укажите URL фронтенда Vercel.

После этого пользователь открывает Mini App прямо из Telegram.

## Production notes

- Не кладите пользовательские API-ключи во frontend.
- `ALLOW_DEV_AUTH=false` на продакшене.
- Для постоянного хранения видео лучше подключить S3/R2. Сейчас видео сохраняется в `media/`, этого хватает для VPS и старта, но Railway может потерять файлы после redeploy/restart.
- AIGate video endpoint синхронный, поэтому backend держит долгий запрос до 15 минут.
