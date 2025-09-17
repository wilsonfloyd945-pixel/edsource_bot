import os
import asyncio
import logging
import json
import re
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
import httpx

# -------------------- ЛОГИ --------------------
logger = logging.getLogger("uvicorn.error")

# -------------------- ENV ---------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
Z_AI_API_KEY = os.environ["Z_AI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "default_secret")

# Бесплатная модель Z.AI
ZAI_MODEL = os.environ.get("Z_AI_MODEL", "glm-4.5-Flash")

# -------------------- APP ---------------------
app = FastAPI()
http_client: Optional[httpx.AsyncClient] = None

# Простая "сессия" по чату (in-memory)
SESSIONS: Dict[int, Dict[str, Any]] = {}

# Тексты интерфейса
MENU_BTN_FORMAT = "Оформить источник внутри текста"
PROMPT_ENTER_SOURCE = (
    "Пришлите, пожалуйста, источник с гиперссылкой (URL) и данными. "
    "Я оформлю его строго в одну строку нужного вида."
)
CANCEL_MSG = "Режим форматирования отключён. Чтобы начать заново — /menu"
HELP_MSG = (
    "Доступные команды:\n"
    "/start — приветствие и меню\n"
    "/menu — показать меню\n"
    "/cancel — выйти из режима\n\n"
    f"Кнопка меню: «{MENU_BTN_FORMAT}» — режим форматирования источника."
)

# Клавиатура меню (Reply Keyboard)
def menu_keyboard() -> Dict[str, Any]:
    return {
        "keyboard": [[{"text": MENU_BTN_FORMAT}]],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

# -------------------- ПРОМПТ ДЛЯ МОДЕЛИ ---------------------
SYSTEM_PROMPT_FORMATTER = """
Ты — форматтер ссылок. Твоя задача: из входных данных о публикации
вывести СТРОГО одну строку вида:

(ССЫЛКА 'НАЗВАНИЕ // ИЗДАНИЕ. — ГОД. — Vol. X, No. Y. — P. N–M. — DOI: Z')

Правила:
1) ВСЕГДА начинай со ссылки (URL). Если во входе есть DOI без ссылки, используй формат: https://doi.org/<DOI>.
2) ИГНОРИРУЙ авторов полностью (их в ответе не должно быть).
3) Внутри одинарных кавычек укажи строго: «Название // Издание. — Год.»
   Если есть том/выпуск/страницы/DOI — добавь их через тире (—) как в примере.
4) Не добавляй НИЧЕГО, кроме этой одной строки (никаких пояснений, приветствий, кода, кавычек вокруг всей строки и т.п.).
5) Сохраняй регистр и пунктуацию названия/журнала как во входных данных.
6) Ничего не выдумывай. Если какого-то элемента нет — просто не пиши его.
7) Если нет ни URL, ни DOI — ответь ровно: Требуется гиперссылка на источник.

Пример:
Вход:
Ardisson Korat A. V., ... DOI: 10.1016/j.ajcnut.2023.11.010.
Ссылка: https://linkinghub.elsevier.com/retrieve/pii/S0002916523662823

ОТВЕТ (строго одна строка):
(https://linkinghub.elsevier.com/retrieve/pii/S0002916523662823 'Dietary protein intake in midlife in relation to healthy aging - results from the prospective Nurses' Health Study cohort // The American Journal of Clinical Nutrition. — 2024. — Vol. 119, No. 2. — P. 271-282. — DOI: 10.1016/j.ajcnut.2023.11.010')
""".strip()


# -------------------- LIFECYCLE ---------------------
@app.on_event("startup")
async def on_startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=30)


@app.on_event("shutdown")
async def on_shutdown():
    global http_client
    if http_client is not None:
        await http_client.aclose()
        http_client = None


# -------------------- UTILS ---------------------
async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None):
    send_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tr = await http_client.post(send_url, json=payload)
        if tr.is_error:
            logger.error(f"Telegram sendMessage error {tr.status_code}: {tr.text[:300]}")
    except Exception as e:
        logger.exception(f"Telegram sendMessage exception: {e}")


def first_formatted_line(text: str) -> str:
    """
    Страховка: если модель вдруг вернёт лишний текст,
    вытащим первую подходящую строку вида: (http... '...').
    """
    text = text.strip()
    # Оставим только первую строку
    first = text.splitlines()[0].strip() if "\n" in text else text

    # Попробуем матчинги:
    m = re.search(r"\((https?://[^ \t'()]+)\s+'([^']+)'\)", first)
    if m:
        return first

    # Если модель сказала про отсутствие ссылки:
    if "Требуется гиперссылка на источник" in text:
        return "Требуется гиперссылка на источник"

    # В худшем случае обрежем до 1 строки и вернём
    return first


async def call_zai(messages: list) -> str:
    """
    Вызов Z.AI с ретраями.
    """
    zai_url = "https://api.z.ai/api/paas/v4/chat/completions"
    api_key = Z_AI_API_KEY.strip().replace("\n", "").replace("\r", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en",
    }
    data = {
        "model": ZAI_MODEL,
        "messages": messages,
        "temperature": 0.2,   # минимальная "креативность" для стабильного формата
        "stream": False,
    }

    max_attempts = 3
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
                return "Извините, модель вернула пустой ответ."
            return reply

        except httpx.HTTPStatusError as he:
            status = he.response.status_code if he.response else "?"
            body = he.response.text[:500] if he.response else ""
            logger.error(f"Z.AI HTTP {status}: {body}")
            return f"Извините, сервис перегружен (HTTP {status}). Попробуйте ещё раз позже."

        except (httpx.RequestError, ValueError) as re_err:
            logger.exception(f"Z.AI request/json error: {re_err}")
            if attempt < max_attempts:
                await asyncio.sleep(2 * attempt)
                continue
            return "Не получается связаться с моделью. Попробуйте ещё раз."

        except Exception as e:
            logger.exception(f"Unexpected error: {e}")
            return "Непредвиденная ошибка. Мы уже разбираемся."

    return "Не удалось получить ответ."


# -------------------- ROUTES ---------------------
@app.get("/")
def health():
    return {"ok": True}


@app.post("/webhook/{path_secret}")
async def tg_webhook(request: Request, path_secret: str):
    if path_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    update = await request.json()
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"status": "ignored"}

    text = (msg.get("text") or msg.get("caption") or "").strip()

    # Обработка команд
    if text.startswith("/start"):
        SESSIONS.pop(chat_id, None)
        await tg_send_message(
            chat_id,
            "Привет! Я помогу оформить источник внутри текста.\n\n" + HELP_MSG,
            reply_markup=menu_keyboard(),
        )
        return {"status": "ok"}

    if text.startswith("/menu"):
        await tg_send_message(
            chat_id,
            "Выберите действие:",
            reply_markup=menu_keyboard(),
        )
        return {"status": "ok"}

    if text.startswith("/cancel"):
        SESSIONS.pop(chat_id, None)
        await tg_send_message(chat_id, CANCEL_MSG, reply_markup=menu_keyboard())
        return {"status": "ok"}

    # Нажатие кнопки меню
    if text == MENU_BTN_FORMAT:
        SESSIONS[chat_id] = {"mode": "format_citation"}
        await tg_send_message(
            chat_id,
            "Режим: *Оформить источник внутри текста*.\n\n"
            + PROMPT_ENTER_SOURCE,
            reply_markup=menu_keyboard(),
        )
        return {"status": "ok"}

    # Если активен режим форматирования — отправляем в модель с системным промптом
    session = SESSIONS.get(chat_id) or {}
    if session.get("mode") == "format_citation":
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_FORMATTER},
            {"role": "user", "content": text},
        ]
        raw = await call_zai(messages)
        formatted = first_formatted_line(raw)
        # Без лишних строк/разметки
        if len(formatted) > 4096:
            formatted = formatted[:4090] + "…"
        await tg_send_message(chat_id, formatted)
        return {"status": "sent"}

    # По умолчанию — обычный чат с моделью (без системного промпта)
    if not text:
        return {"status": "ignored"}

    # Базовый диалог
    messages = [
        {"role": "user", "content": text},
    ]
    raw = await call_zai(messages)
    if len(raw) > 4096:
        raw = raw[:4090] + "…"
    await tg_send_message(chat_id, raw)
    return {"status": "sent"}
