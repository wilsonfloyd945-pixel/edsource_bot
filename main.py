import os
import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
import httpx

# --- Настройка логгера ---
logger = logging.getLogger("uvicorn.error")

# --- Переменные окружения ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
Z_AI_API_KEY = os.environ["Z_AI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "default_secret")

# --- Приложение ---
app = FastAPI()

# --- Глобальный HTTP-клиент (создаём на старте, закрываем на остановке) ---
http_client: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def on_startup():
    global http_client
    # Один клиент на всё приложение — быстрее и стабильнее (переиспользуются соединения)
    http_client = httpx.AsyncClient(timeout=30)


@app.on_event("shutdown")
async def on_shutdown():
    global http_client
    if http_client is not None:
        await http_client.aclose()
        http_client = None


@app.get("/")
def health():
    return {"ok": True}


@app.post("/webhook/{path_secret}")
async def tg_webhook(request: Request, path_secret: str):
    # Проверяем секрет из URL пути
    if path_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()

    # Берём текст либо из message.text, либо из caption (на случай фото/документов с подписью)
    msg = update.get("message") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat_id = msg.get("chat", {}).get("id")

    if not (text and chat_id):
        return {"status": "ignored"}

    # --- Подготовка запроса к Z.AI ---
    zai_url = "https://api.z.ai/api/paas/v4/chat/completions"
    api_key = Z_AI_API_KEY.strip().replace("\n", "").replace("\r", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en",
    }
    data = {
        # Бесплатная модель
        "model": "glm-4.5-Flash",
        "messages": [{"role": "user", "content": text}],
        "temperature": 0.6,
        "stream": False,
    }

    reply = None
    max_attempts = 3

    # --- Ретраи с бэкоффом для 429/5xx ---
    for attempt in range(1, max_attempts + 1):
        try:
            r = await http_client.post(zai_url, headers=headers, json=data)
            if r.status_code in (429, 502, 503, 504):
                logger.warning(f"Z.AI transient error {r.status_code}: {r.text[:300]}")
                if attempt < max_attempts:
                    await asyncio.sleep(2 * attempt)  # 2s, 4s
                    continue
            r.raise_for_status()

            payload = r.json()
            reply = (
                payload.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            reply = (reply or "").strip()
            if not reply:
                reply = "Извините, модель вернула пустой ответ."
            break

        except httpx.HTTPStatusError as he:
            status = he.response.status_code if he.response else "?"
            body = he.response.text[:500] if he.response else ""
            logger.error(f"Z.AI HTTP {status}: {body}")
            reply = f"Извините, сервис перегружен (HTTP {status}). Попробуйте ещё раз чуть позже."
            break

        except (httpx.RequestError, ValueError) as re:
            # RequestError — сеть/таймаут, ValueError — проблемы с JSON
            logger.exception(f"Z.AI request/json error: {re}")
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
                continue
            reply = "Не получается связаться с моделью. Попробуйте ещё раз."

        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            reply = "Непредвиденная ошибка. Мы уже разбираемся."
            break

    # --- Ограничение длины сообщения для Telegram ---
    if reply is None or not isinstance(reply, str):
        reply = "Извините, произошла ошибка."
    if len(reply) > 4096:
        reply = reply[:4090] + "…"

    # --- Отправка ответа в Telegram ---
    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        tr = await http_client.post(
            send_url, json={"chat_id": chat_id, "text": reply}
        )
        if tr.is_error:
            logger.error(f"Telegram sendMessage error {tr.status_code}: {tr.text[:300]}")
    except Exception as e:
        logger.exception(f"Telegram sendMessage exception: {e}")

    return {"status": "sent"}
