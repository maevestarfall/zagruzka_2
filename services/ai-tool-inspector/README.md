# AI Tool Inspector

Система верификации инструментов ИИ-агентов с Telegram ботом.

## Как работает связь с ИИ

ИИ-агент сам приходит к тебе — API-ключ НЕ нужен:

1. Сгенерируй токен: POST /api/tokens/:modelId
2. Дай ИИ probe URL: GET /api/probe/:modelId?token=xxx
3. ИИ забирает задания и твои сообщения
4. ИИ верифицирует: POST /api/probe/:modelId/verify?token=xxx
5. Ты пишешь ИИ: POST /api/messages/:modelId
6. ИИ отвечает: POST /api/probe/:modelId/reply?token=xxx

## Быстрый старт

```bash
pnpm install
cp env.example .env
# Минимум: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
# Опционально для прямого чата: ANTHROPIC_API_KEY / OPENAI_API_KEY

node server.mjs   # API сервер на :3001
node bot.mjs      # Telegram бот (другой терминал)
```

## API
- GET  /api/health                             — статус сервера
- GET  /api/models                             — все модели
- POST /api/tokens/:modelId                    — создать токен для ИИ
- GET  /api/probe/:modelId?token=              — ИИ забирает задания + сообщения
- POST /api/probe/:modelId/verify?token=       — ИИ верифицирует инструмент
- POST /api/probe/:modelId/reply?token=        — ИИ отвечает на сообщение
- POST /api/messages/:modelId                  — написать сообщение для ИИ
- GET  /api/messages/:modelId                  — история переписки
- POST /api/chat                               — прямой чат через AI API (нужен ключ)
- GET  /api/summary                            — сводка с trustScore
- GET  /docs                                   — Swagger UI

## Деплой на Render.com
1. Залей на GitHub
2. render.com → New → Blueprint → выбери репо
3. Dashboard → Environment → TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

## Деплой Docker (Oracle Always Free / VPS)
```bash
docker build -t ai-tool-inspector .
docker run -p 3001:3001 \
  -e TELEGRAM_BOT_TOKEN=xxx \
  -e TELEGRAM_CHAT_ID=xxx \
  -e SERVICE_URL=https://your-domain.com \
  ai-tool-inspector
```

## Telegram бот
/start, /stats, /services, /addserver, /link, /models, /tokens
