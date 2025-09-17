import os
import asyncio
import logging
import json
import re
import random
from time import monotonic
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, HTTPException
import httpx

# -------------------- ЛОГИ --------------------
logger = logging.getLogger("uvicorn.error")

# -------------------- ENV ---------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
Z_AI_API_KEY = os.environ["Z_AI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "default_secret")

# Бесплатная модель Z.AI по умолчанию
ZAI_MODEL = os.environ.get("Z_AI_MODEL", "glm-4.5-Flash")

# Предел параллельных запросов к модели (очень важно, чтобы не ловить 429 High concurrency)
# Для бесплатного пула обычно безопасно 1–2. При большом наплыве лучше 1.
ZAI_CONCURRENCY_LIMIT = int(os.environ.get("ZAI_CONCURRENCY_LIMIT", "2"))

# Анти-спам по чату (секунды между запросами от одного пользователя)
PER_CHAT_COOLDOWN = float(os.environ.get("PER_CHAT_COOLDOWN", "0.7"))

# Таймауты клиента HTTP: увеличенный read и pool для «тяжёлых» ответов модели
HTTPX_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=60.0)

# -------------------- APP ---------------------
app = FastAPI()
http_client: Optional[httpx.AsyncClient] = None

# Ограничение одновременных обращений к модели
zai_semaphore = asyncio.Semaphore(ZAI_CONCURRENCY_LIMIT)

# Простая "сессия" по чату (in-memory)
SESSIONS: Dict[int, Dict[str, Any]] = {}

# Анти-спам трекер по чатам
LAST_HIT: dict[int, float] = {}

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
8) Если ты не видишь название статьи или место публикации, или сомневаешься, что это оно, проси у пользователя уточнить, прежде чем дать ответ.


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
    http_client = httpx.AsyncClient(timeout=HTTPX_TIMEOUT)


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
    text = (text or "").strip()
    first = text.splitlines()[0].strip() if "\n" in text else text

    m = re.search(r"\((https?://[^ \t'()]+)\s+'([^']+)'\)", first)
    if m:
        return first

    if "Требуется гиперссылка на источник" in text:
        return "Требуется гиперссылка на источник"

    return first or "Извините, модель вернула пустой ответ."


def _parse_retry_after(headers: httpx.Headers) -> float | None:
    # Retry-After может быть в секундах или в формате даты
    ra = headers.get("Retry-After")
    if not ra:
        return None
    try:
        return float(ra)
    except ValueError:
        try:
            dt = datetime.strptime(ra, "%a, %d %b %Y %H:%M:%S %Z")
            return max(0.0, (dt - datetime.utcnow()).total_seconds())
        except Exception:
            return None


async def call_zai(messages: list) -> str:
    """
    Вызов Z.AI с ретраями, backoff, учётом Retry-After и ограничением параллелизма.
    """
    zai_url = "https://api.z.ai/api/paas/v4/chat/completions"
    api_key = Z_AI_API_KEY.strip().replace("\n", "").replace("\r", "")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en",
    }
    data = {
        "model": ZAI_MODEL,          # glm-4.5-Flash по умолчанию
        "messages": messages,
        "temperature": 0.2,          # минимальная креативность для стабильного формата
        "stream": False,
    }

    max_attempts = 4
    base_sleep = 1.5

    # Важный момент: ограничиваем конкурентность, чтобы не ловить массовые 429
    async with zai_semaphore:
        for attempt in range(1, max_attempts + 1):
            try:
                r = await http_client.post(zai_url, headers=headers, json=data)

                # Временные ошибки и лимиты — дадим шанс на повтор
                if r.status_code in (429, 502, 503, 504):
                    ra = _parse_retry_after(r.headers) or (base_sleep * attempt)
                    ra *= random.uniform(0.8, 1.2)  # небольшой джиттер
                    logger.warning(
                        f"Z.AI transient {r.status_code}; retry in ~{ra:.2f}s; body: {r.text[:300]}"
                    )
                    if attempt < max_attempts:
                        await asyncio.sleep(ra)
                        continue

                r.raise_for_status()

                payload = r.json()
                reply = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                reply = (reply or "").strip()
                return reply or "Извините, модель вернула пустой ответ."

            except httpx.ReadTimeout:
                # Модель долго отвечает — даём ещё попытки с увеличением ожидания
                if attempt < max_attempts:
                    ra = (base_sleep * (attempt + 1)) * random.uniform(0.8, 1.2)
                    logger.warning(f"Z.AI read timeout; retry in ~{ra:.2f}s")
                    await asyncio.sleep(ra)
                    continue
                return "Сервис отвечает дольше обычного. Попробуйте ещё раз чуть позже."

            except httpx.HTTPStatusError as he:
                status = he.response.status_code if he.response else "?"
                body = he.response.text[:500] if he.response else ""
                logger.error(f"Z.AI HTTP {status}: {body}")
                return f"Извините, сервис перегружен (HTTP {status}). Попробуйте ещё раз позже."

            except (httpx.RequestError, ValueError) as re_err:
                logger.exception(f"Z.AI request/json error: {re_err}")
                if attempt < max_attempts:
                    ra = (base_sleep * attempt) * random.uniform(0.8, 1.2)
                    await asyncio.sleep(ra)
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
    if not text:
        return {"status": "ignored"}

    # --- Анти-спам по чату ---
    now = monotonic()
    last = LAST_HIT.get(chat_id, 0.0)
    if now - last < PER_CHAT_COOLDOWN:
        # Тихо игнорим слишком частые хиты, чтобы не накапливать очередь
        return {"status": "rate_limited"}
    LAST_HIT[chat_id] = now

    # --- Команды ---
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

    # --- Нажатие кнопки меню ---
    if text == MENU_BTN_FORMAT:
        SESSIONS[chat_id] = {"mode": "format_citation"}
        await tg_send_message(
            chat_id,
            "Режим: *Оформить источник внутри текста*.\n\n" + PROMPT_ENTER_SOURCE,
            reply_markup=menu_keyboard(),
        )
        return {"status": "ok"}

    # --- Режим форматирования источника ---
    session = SESSIONS.get(chat_id) or {}
    if session.get("mode") == "format_citation":
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_FORMATTER},
            {"role": "user", "content": text},
        ]
        raw = await call_zai(messages)
        formatted = first_formatted_line(raw)
        if len(formatted) > 4096:
            formatted = formatted[:4090] + "…"
        await tg_send_message(chat_id, formatted)
        return {"status": "sent"}

    # --- Базовый диалог с моделью (если не в режиме форматтера) ---
    messages = [{"role": "user", "content": text}]
    raw = await call_zai(messages)
    if len(raw) > 4096:
        raw = raw[:4090] + "…"
    await tg_send_message(chat_id, raw)
    return {"status": "sent"}
