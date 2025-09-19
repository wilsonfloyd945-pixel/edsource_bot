import os
import asyncio
import logging
import re
import random
from time import monotonic
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import httpx
from fastapi.responses import JSONResponse

# -------------------- ЛОГИ --------------------
logger = logging.getLogger("uvicorn.error")

# -------------------- ENV ---------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
Z_AI_API_KEY = os.environ["Z_AI_API_KEY"]
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "default_secret")

# Бесплатная модель Z.AI по умолчанию
ZAI_MODEL = os.environ.get("Z_AI_MODEL", "glm-4.5-Flash")

# Предел параллельных запросов к модели (free-тариф любит 1–2)
ZAI_CONCURRENCY_LIMIT = int(os.environ.get("ZAI_CONCURRENCY_LIMIT", "2"))

# Анти-спам по чату (секунды между запросами от одного пользователя)
PER_CHAT_COOLDOWN = float(os.environ.get("PER_CHAT_COOLDOWN", "0.7"))

# Таймауты клиента HTTP
HTTPX_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=60.0)

# Сторож-таймаут на обращение к модели (сек)
MODEL_WATCHDOG_SECONDS = int(os.environ.get("MODEL_WATCHDOG_SECONDS", "25"))

# -------------------- APP ---------------------
app = FastAPI()
http_client: Optional[httpx.AsyncClient] = None

# Ограничение одновременных обращений к модели (можно переключать «безопасный режим»)
zai_semaphore = asyncio.Semaphore(ZAI_CONCURRENCY_LIMIT)
SAFE_MODE = False  # когда True — семафор = 1

# Состояния по чатам
SESSIONS: Dict[int, Dict[str, Any]] = {}
LAST_HIT: dict[int, float] = {}

# -------------------- ТЕКСТЫ И КНОПКИ ---------------------
MENU_BTN_FORMAT = "Оформить источник внутри текста"
BTN_CLEAR = "🧹 Очистить контекст"
BTN_MENU = "🏠 В меню"
BTN_RESTART = "🔄 Перезапуск"
BTN_FIX = "🛠 Починить сбои"

PROMPT_ENTER_SOURCE = (
    "Пришлите, пожалуйста, источник с гиперссылкой (URL) и данными. "
    "Можно по частям, в любом порядке. Я соберу и оформлю в одну строку."
)
CANCEL_MSG = "Режим форматирования отключён. Чтобы начать заново — /menu"
HELP_MSG = (
    "Доступные команды:\n"
    "/start — приветствие и меню\n"
    "/menu — показать меню\n"
    "/cancel — выйти из режима\n\n"
    f"Кнопка меню: «{MENU_BTN_FORMAT}» — режим форматирования источника."
)

def menu_keyboard() -> Dict[str, Any]:
    # Всегда показываем полезные кнопки управления
    return {
        "keyboard": [
            [{"text": MENU_BTN_FORMAT}],
            [{"text": BTN_CLEAR}, {"text": BTN_MENU}],
            [{"text": BTN_RESTART}, {"text": BTN_FIX}],
        ],
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
8) Если ты не видишь название статьи или место публикации, или сомневаешься, что это оно, попроси у пользователя уточнить, прежде чем дать ответ.
""".strip()

# -------------------- ЖИЗНЕННЫЙ ЦИКЛ ---------------------
@app.on_event("startup")
async def on_startup():
    await _reinit_http_client()

@app.on_event("shutdown")
async def on_shutdown():
    global http_client
    if http_client is not None:
        await http_client.aclose()
        http_client = None

async def _reinit_http_client():
    """Переинициализация HTTP-клиента (используется и кнопкой «Починить сбои»)."""
    global http_client
    try:
        if http_client is not None:
            await http_client.aclose()
    except Exception:
        pass
    http_client = httpx.AsyncClient(timeout=HTTPX_TIMEOUT)

def _set_safe_mode(enabled: bool):
    """Вкл/выкл безопасный режим (семафор на 1)."""
    global SAFE_MODE, zai_semaphore
    SAFE_MODE = enabled
    limit = 1 if SAFE_MODE else ZAI_CONCURRENCY_LIMIT
    zai_semaphore = asyncio.Semaphore(limit)
    logger.warning(f"SAFE_MODE={'ON' if SAFE_MODE else 'OFF'}; concurrency={limit}")

# -------------------- TELEGRAM HELPERS ---------------------
async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> Optional[int]:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tr = await http_client.post(url, json=payload)
        if tr.is_error:
            logger.error(f"Telegram sendMessage error {tr.status_code}: {tr.text[:300]}")
            return None
        data = tr.json()
        return data.get("result", {}).get("message_id")
    except Exception as e:
        logger.exception(f"Telegram sendMessage exception: {e}")
        return None

async def tg_edit_message(chat_id: int, message_id: int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    try:
        tr = await http_client.post(url, json=payload)
        if tr.is_error:
            logger.error(f"Telegram editMessageText error {tr.status_code}: {tr.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.exception(f"Telegram editMessageText exception: {e}")
        return False

async def tg_send_action(chat_id: int, action: str = "typing"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendChatAction"
    payload = {"chat_id": chat_id, "action": action}
    try:
        await http_client.post(url, json=payload)
    except Exception:
        pass

# -------------------- TEXT UTILS ---------------------
_URL_RE = re.compile(r"(https?://[^\s<>')]+)", re.IGNORECASE)


# безопасный запуск фоновых корутин
def fire_and_forget(coro):
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        asyncio.get_event_loop().create_task(coro)

async def handle_update_safe(update: dict):
    """Вся логика обработки апдейта. Любые исключения ловим внутри, чтобы не ронять процесс."""
    try:
        chat_id = None
        message = update.get("message") or update.get("edited_message") or update.get("callback_query", {}).get("message")
        if message:
            chat_id = message["chat"]["id"]
        # 👉 здесь вызывай свою существующую функцию обработки апдейтов,
        # например: await process_update(update)
        await process_update(update)  # <-- используй то, что уже было у тебя
    except Exception:
        logger.exception("handle_update_safe error")

@app.post("/webhook/{path_secret}")
async def telegram_webhook(path_secret: str, request: Request, background_tasks: BackgroundTasks):
    # Проверяем секрет и ВСЕГДА отвечаем быстро
    if path_secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    try:
        update = await request.json()
    except Exception:
        # даже если не смогли распарсить — отвечаем 200, чтобы не было 503
        logger.exception("Bad Telegram update payload")
        return JSONResponse({"ok": True})

    # ставим задачу на фон (ответ 200 уйдет мгновенно)
    background_tasks.add_task(handle_update_safe, update)
    return JSONResponse({"ok": True})  # 👈 мгновенный 200

def extract_url_and_meta(text: str) -> Tuple[Optional[str], str]:
    text = (text or "").strip()
    if not text:
        return None, ""
    m = _URL_RE.search(text)
    if not m:
        return None, text
    url = m.group(1)
    meta = (text[:m.start()] + text[m.end():]).strip()
    return url, meta

def first_formatted_line(
    text: str,
    fallback_link: Optional[str] = None,
    fallback_meta: Optional[str] = None,
) -> str:
    """
    Нормализуем ответ модели к виду: (URL 'META')
    """
    text = (text or "").strip()
    first = text.splitlines()[0].strip() if "\n" in text else text

    m_ok = re.match(r"^\((https?://[^\s'()]+)\s+'([^']+)'\)$", first)
    if m_ok:
        return first

    m_noparens = re.match(r"^(https?://[^\s'()]+)\s+'([^']+)'$", first)
    if m_noparens:
        url, quoted = m_noparens.group(1), m_noparens.group(2)
        return f"({url} '{quoted}')"

    m_url_only = re.search(r"(https?://[^\s'()]+)", first)
    if m_url_only and fallback_meta:
        url = m_url_only.group(1)
        meta = fallback_meta.strip()
        if meta:
            return f"({url} '{meta}')"

    if "Требуется гиперссылка на источник" in text:
        return "Требуется гиперссылка на источник"

    if fallback_link and fallback_meta:
        return f"({fallback_link.strip()} '{fallback_meta.strip()}')"

    return first or "Извините, модель вернула пустой ответ."

# -------------------- Z.AI CALL ---------------------
def _parse_retry_after(headers: httpx.Headers) -> float | None:
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
        "model": ZAI_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }

    max_attempts = 4
    base_sleep = 1.5

    async with zai_semaphore:
        for attempt in range(1, max_attempts + 1):
            try:
                r = await http_client.post(zai_url, headers=headers, json=data)

                if r.status_code in (429, 502, 503, 504):
                    ra = _parse_retry_after(r.headers) or (base_sleep * attempt)
                    ra *= random.uniform(0.8, 1.2)
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


async def tg_delete_message(chat_id: int, message_id: int) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        tr = await http_client.post(url, json=payload)
        if tr.is_error:
            logger.error(f"Telegram deleteMessage error {tr.status_code}: {tr.text[:300]}")
            return False
        return True
    except Exception as e:
        logger.exception(f"Telegram deleteMessage exception: {e}")
        return False

# -------------------- ROUTES ---------------------
@app.get("/")
def health():
    return {"ok": True, "safe_mode": SAFE_MODE, "concurrency": 1 if SAFE_MODE else ZAI_CONCURRENCY_LIMIT}

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

    # Анти-спам по чату
    now = monotonic()
    last = LAST_HIT.get(chat_id, 0.0)
    if now - last < PER_CHAT_COOLDOWN:
        return {"status": "rate_limited"}
    LAST_HIT[chat_id] = now

    # ----- КНОПКИ-УПРАВЛЕНИЯ -----
    if text == BTN_CLEAR:
        sess = SESSIONS.get(chat_id)
        if sess and sess.get("mode") == "format_citation":
            sess["parts"] = {"link": None, "meta": ""}
            await tg_send_message(chat_id, "Контекст очищен. Пришлите ссылку/данные.", reply_markup=menu_keyboard())
        else:
            await tg_send_message(chat_id, "Контекст и так пуст. Нажмите «Оформить источник внутри текста».", reply_markup=menu_keyboard())
        return {"status": "ok"}

    if text == BTN_MENU:
        SESSIONS.pop(chat_id, None)
        await tg_send_message(chat_id, "Вы в меню. Выберите действие:", reply_markup=menu_keyboard())
        return {"status": "ok"}

    if text == BTN_RESTART:
        SESSIONS.pop(chat_id, None)
        await tg_send_message(chat_id, "Перезапуск. Готов к работе.\n\n" + HELP_MSG, reply_markup=menu_keyboard())
        return {"status": "ok"}

    if text == BTN_FIX:
        # Переключаем безопасный режим и переинициализируем клиент.
        _set_safe_mode(not SAFE_MODE)
        LAST_HIT.clear()
        await _reinit_http_client()
        state = "включён (конкурентность = 1)" if SAFE_MODE else f"выключен (конкурентность = {ZAI_CONCURRENCY_LIMIT})"
        await tg_send_message(chat_id, f"🛠 Безопасный режим {state}. Клиент сети переинициализирован.", reply_markup=menu_keyboard())
        return {"status": "ok"}

    # ----- КОМАНДЫ -----
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

    # ----- НАЖАТИЕ ОСНОВНОЙ КНОПКИ МЕНЮ -----
    if text == MENU_BTN_FORMAT:
        SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}
        await tg_send_message(
            chat_id,
            "Режим: *Оформить источник внутри текста*.\n\n" + PROMPT_ENTER_SOURCE,
            reply_markup=menu_keyboard(),
        )
        return {"status": "ok"}

    # ----- РЕЖИМ ФОРМАТТЕРА С КОНТЕКСТОМ -----
    session = SESSIONS.get(chat_id) or {}
    if session.get("mode") == "format_citation":
        parts = session.setdefault("parts", {"link": None, "meta": ""})

        url_in, meta_in = extract_url_and_meta(text)
        if url_in:
            parts["link"] = url_in.strip()
        if meta_in:
            parts["meta"] = (parts["meta"] + "\n" + meta_in).strip() if parts["meta"] else meta_in

        if not parts["link"] and not parts["meta"]:
            await tg_send_message(chat_id, "Нужны данные об источнике и ссылка. Пришлите любую часть.", reply_markup=menu_keyboard())
            return {"status": "ok"}
        if not parts["link"]:
            await tg_send_message(chat_id, "Есть данные. Пришлите, пожалуйста, гиперссылку (URL) на источник.", reply_markup=menu_keyboard())
            return {"status": "ok"}
        if not parts["meta"]:
            await tg_send_message(chat_id, "Ссылка получена. Пришлите, пожалуйста, название статьи, издание, год и т. п.", reply_markup=menu_keyboard())
            return {"status": "ok"}

        await tg_send_action(chat_id, "typing")
        placeholder_id = await tg_send_message(chat_id, "Оформляю…", reply_markup=menu_keyboard())

        user_payload = f"{parts['meta']}\n{parts['link']}".strip()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_FORMATTER},
            {"role": "user", "content": user_payload},
        ]

        final_text = None
        error_text = None
        try:
            raw = await asyncio.wait_for(call_zai(messages), timeout=MODEL_WATCHDOG_SECONDS)
            formatted = first_formatted_line(
                raw,
                fallback_link=parts.get("link"),
                fallback_meta=parts.get("meta"),
            )
            if len(formatted) > 4096:
                formatted = formatted[:4090] + "…"
            final_text = formatted

        except asyncio.TimeoutError:
            error_text = "Сервис отвечает дольше обычного. Попробуйте ещё раз чуть позже."
        except Exception as e:
            logger.exception(f"format_citation pipeline error: {e}")
            error_text = "Не удалось оформить источник. Попробуйте ещё раз."

        out = final_text or error_text or "Неизвестная ошибка."
        if placeholder_id:
            ok = await tg_edit_message(chat_id, placeholder_id, out)
            if not ok:
                # если редактирование запрещено: удалим плейсхолдер и пришлём результат заново
                deleted = await tg_delete_message(chat_id, placeholder_id)
                await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
        else:
            await tg_send_message(chat_id, out, reply_markup=menu_keyboard())

        # Оставляем режим включённым, но очищаем части — можно сразу оформлять следующий
        SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}
        return {"status": "sent"}

    # ----- ОБЫЧНЫЙ ДИАЛОГ -----
    await tg_send_action(chat_id, "typing")
    placeholder_id = await tg_send_message(chat_id, "Думаю…", reply_markup=menu_keyboard())
    messages = [{"role": "user", "content": text}]

    final_text = None
    error_text = None
    try:
        raw = await asyncio.wait_for(call_zai(messages), timeout=MODEL_WATCHDOG_SECONDS)
        final_text = raw if raw else "Извините, модель вернула пустой ответ."
        if len(final_text) > 4096:
            final_text = final_text[:4090] + "…"
    except asyncio.TimeoutError:
        error_text = "Сервис отвечает дольше обычного. Попробуйте ещё раз чуть позже."
    except Exception as e:
        logger.exception(f"chat pipeline error: {e}")
        error_text = "Произошла ошибка. Попробуйте ещё раз."

    out = final_text or error_text or "Неизвестная ошибка."
    if placeholder_id:
        ok = await tg_edit_message(chat_id, placeholder_id, out)
        if not ok:
            await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
    else:
        await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
    return {"status": "sent"}




