import os
import re
import asyncio
import logging
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse, PlainTextResponse

# ------------------------
# Конфиг / ENV
# ------------------------
logger = logging.getLogger("uvicorn.error")

def env_required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

TELEGRAM_TOKEN   = env_required("TELEGRAM_TOKEN")
Z_AI_API_KEY     = env_required("Z_AI_API_KEY")
WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "amagh743").strip()

Z_AI_MODEL       = os.environ.get("Z_AI_MODEL", "glm-4.5-Flash").strip()
ZAI_CONCURRENCY_LIMIT = int(os.environ.get("ZAI_CONCURRENCY_LIMIT", "2"))
PER_CHAT_COOLDOWN     = float(os.environ.get("PER_CHAT_COOLDOWN", "0.7"))
MODEL_WATCHDOG_SECONDS = int(os.environ.get("MODEL_WATCHDOG_SECONDS", "25"))

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"

# ------------------------
# Глобальные объекты
# ------------------------
app = FastAPI()
http_client: Optional[httpx.AsyncClient] = None
zai_semaphore = asyncio.Semaphore(ZAI_CONCURRENCY_LIMIT)

# user state: {chat_id: {"mode": str, "parts": {"link": Optional[str], "meta": str}}}
SESSIONS: Dict[int, Dict[str, Any]] = {}

# Анти-спам по пользователю
LAST_USED_AT: Dict[int, float] = {}

# ------------------------
# Тексты и клавиатуры
# ------------------------
SYSTEM_PROMPT_FORMATTER = (
    "Ты оформляешь научные/медийные источники ВНУТРИ ТЕКСТА строго в формате:\n"
    "(<гиперссылка> '<полный источник: название, издание/журнал, год, том/номер, страницы, DOI если есть>')\n"
    "Только одна строка, без лишнего текста. Никаких авторов в начале. Если данных не хватает — не выдумывай."
)

def menu_keyboard() -> Dict[str, Any]:
    # tg reply_markup
    return {
        "keyboard": [
            [{"text": "📚 Оформить источник внутри текста"}],
            [{"text": "🔄 Очистить контекст"}, {"text": "🏠 Меню"}],
            [{"text": "🛠 Починить сбои"}, {"text": "♻️ Перезапуск"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

WELCOME = (
    "Привет! Я помогу оформить источник в виде ссылки внутри текста.\n\n"
    "Нажми «📚 Оформить источник внутри текста», а затем пришли:\n"
    "— либо полный источник С ГИПЕРССЫЛКОЙ,\n"
    "— либо по очереди: сначала источник (без авторов в начале), потом гиперссылку.\n"
    "Отвечаю одной строкой в формате:\n"
    "(https://ссылка 'Название… // Журнал — Год — Том, No. — Страницы — DOI: ...')"
)

# ------------------------
# Утилиты Telegram
# ------------------------
async def tg_call(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        r = await http_client.post(url, json=payload, timeout=15)
        if r.status_code >= 400:
            logger.error(f"Telegram {method} error {r.status_code}: {r.text}")
        data = r.json()
        return data if data.get("ok") else None
    except Exception:
        logger.exception(f"Telegram call failed: {method}")
        return None

async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
    resp = await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_markup,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if resp and resp.get("result"):
        return resp["result"]["message_id"]
    return None

async def tg_edit_message(chat_id: int, message_id: int, text: str) -> bool:
    resp = await tg_call("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return bool(resp and resp.get("ok"))

async def tg_send_action(chat_id: int, action: str = "typing") -> None:
    await tg_call("sendChatAction", {"chat_id": chat_id, "action": action})

# ------------------------
# LLM: Z.AI
# ------------------------
async def call_zai(messages: List[Dict[str, str]]) -> str:
    headers = {
        "Authorization": f"Bearer {Z_AI_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": Z_AI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
    }

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        async with zai_semaphore:
            try:
                r = await http_client.post(ZAI_URL, headers=headers, json=data, timeout=25)
                # хендлим перегруз
                if r.status_code in (429, 502, 503, 504):
                    logger.warning(f"Z.AI transient {r.status_code}: {r.text[:200]}")
                    if attempt < max_attempts:
                        await asyncio.sleep(1.2 * attempt)
                        continue
                r.raise_for_status()
                payload = r.json()
                reply = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return (reply or "").strip() or "Извините, модель вернула пустой ответ."
            except httpx.ReadTimeout:
                if attempt < max_attempts:
                    await asyncio.sleep(1.2 * attempt)
                    continue
                return "Сервис отвечает дольше обычного. Попробуйте ещё раз."
            except httpx.HTTPStatusError as he:
                status = he.response.status_code if he.response else "?"
                body = he.response.text[:500] if he.response else ""
                logger.error(f"Z.AI HTTP {status}: {body}")
                return "Сервис перегружен. Попробуйте ещё раз позже."
            except Exception:
                logger.exception("Z.AI unexpected error")
                if attempt < max_attempts:
                    await asyncio.sleep(1.0 * attempt)
                    continue
                return "Произошла ошибка при обращении к модели."

async def call_llm(messages: List[Dict[str, str]]) -> str:
    # просто обёртка — можно добавить фолбэк-провайдеры при желании
    return await call_zai(messages)

# ------------------------
# Форматирование результата (гарантируем скобки)
# ------------------------
LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

def force_parenthesized(link: Optional[str], meta: Optional[str], model_text: str) -> str:
    """
    Гарантируем формат: (link 'meta')
    1) если модель уже вернула корректно в скобках — вернём как есть;
    2) иначе соберём сами из link+meta;
    3) одинарные кавычки внутри meta экранируем типографским апострофом.
    """
    txt = model_text.strip()
    if txt.startswith("(") and txt.endswith(")") and "'" in txt:
        return txt  # выглядит ок

    lnk = (link or "").strip()
    mt  = (meta or "").strip()

    # Если модель вернула ссылку и автораспознанный заголовок, попробуем выдрать:
    found_link = LINK_RE.search(txt)
    if not lnk and found_link:
        lnk = found_link.group(0)

    if not mt:
        # Вырезаем кавычках/без — берём всё после ссылки
        # Но если ничего внятного — fallback к исходному тексту
        mt = txt

    # нормализуем кавычки
    safe_meta = mt.replace("’", "'").replace("`", "'")
    safe_meta = safe_meta.replace("'", "’")  # внутр. апострофы → типографский
    if lnk:
        return f"({lnk} '{safe_meta}')"
    # если ссылку так и не достали — пусть модельный текст, но в скобках
    return f"({safe_meta})"

def first_formatted_line(model_text: str, fallback_link: Optional[str], fallback_meta: Optional[str]) -> str:
    out = force_parenthesized(fallback_link, fallback_meta, model_text)
    # одна строка
    out = out.replace("\r", " ").replace("\n", " ").strip()
    return out

# ------------------------
# Режим «оформление источника»
# ------------------------
def ensure_session(chat_id: int) -> Dict[str, Any]:
    s = SESSIONS.get(chat_id)
    if not s:
        s = {"mode": "menu", "parts": {"link": None, "meta": ""}}
        SESSIONS[chat_id] = s
    if "parts" not in s:
        s["parts"] = {"link": None, "meta": ""}
    return s

def is_link(text: str) -> bool:
    return bool(LINK_RE.search(text))

async def handle_formatter_message(chat_id: int, text: str) -> None:
    sess = ensure_session(chat_id)
    parts = sess["parts"]
    txt = text.strip()

    if is_link(txt):
        parts["link"] = LINK_RE.search(txt).group(0)
    else:
        # накапливаем «метаданные» источника
        if parts["meta"]:
            parts["meta"] = (parts["meta"] + " " + txt).strip()
        else:
            parts["meta"] = txt

    # проверяем, достаточно ли данных
    if parts.get("link") and parts.get("meta"):
        # ставим плейсхолдер и фоновую задачу
        await tg_send_action(chat_id, "typing")
        placeholder_id = await tg_send_message(chat_id, "Оформляю…", reply_markup=menu_keyboard())
        fire_and_forget(_format_worker(chat_id, parts.copy(), placeholder_id))
    else:
        # просим недостающее
        if not parts.get("link"):
            await tg_send_message(chat_id, "Пришлите гиперссылку на источник (начинается с http/https).", reply_markup=menu_keyboard())
        elif not parts.get("meta"):
            await tg_send_message(chat_id, "Пришлите данные об источнике (название, журнал/место публикации, год, том/номер, страницы, DOI).", reply_markup=menu_keyboard())

async def _format_worker(chat_id: int, parts: Dict[str, Any], placeholder_id: Optional[int]) -> None:
    """
    Фоновая задача: вызывает LLM и редактирует плейсхолдер.
    """
    user_payload = f"{parts.get('meta','')}\n{parts.get('link','')}".strip()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_FORMATTER},
        {"role": "user", "content": user_payload},
    ]

    try:
        raw = await asyncio.wait_for(call_llm(messages), timeout=MODEL_WATCHDOG_SECONDS)
        formatted = first_formatted_line(raw, fallback_link=parts.get("link"), fallback_meta=parts.get("meta"))
        if len(formatted) > 4096:
            formatted = formatted[:4090] + "…"
        out = formatted
    except asyncio.TimeoutError:
        out = "Сервис отвечает дольше обычного. Попробуйте ещё раз."
    except Exception:
        logger.exception("format_worker error")
        out = "Не удалось оформить источник. Попробуйте ещё раз."

    if placeholder_id:
        ok = await tg_edit_message(chat_id, placeholder_id, out)
        if not ok:
            await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
    else:
        await tg_send_message(chat_id, out, reply_markup=menu_keyboard())

    # очищаем введённые части, остаёмся в режиме форматирования
    SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}

# ------------------------
# Фоновые задачи: «fire and forget»
# ------------------------
def fire_and_forget(coro):
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        asyncio.get_event_loop().create_task(coro)

# ------------------------
# Обработка апдейтов Telegram (в фоне)
# ------------------------
async def process_update(update: Dict[str, Any]) -> None:
    """
    Вся логика обработки апдейта (сообщения/кнопки).
    Вызывается в фоне, чтобы вебхук сразу отдавал 200.
    """
    try:
        msg = update.get("message") or update.get("edited_message")
        cb  = update.get("callback_query")

        if cb:
            message = cb.get("message")
            if not message:
                return
            chat_id = message["chat"]["id"]
            data = (cb.get("data") or "").strip()
            if data == "menu":
                SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
                await tg_send_message(chat_id, "Меню:", reply_markup=menu_keyboard())
            return

        if not msg:
            return

        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()

        # простая защита от спама
        now = asyncio.get_event_loop().time()
        last = LAST_USED_AT.get(chat_id, 0.0)
        if now - last < PER_CHAT_COOLDOWN:
            return
        LAST_USED_AT[chat_id] = now

        sess = ensure_session(chat_id)
        mode = sess.get("mode", "menu")

        # системные кнопки
        if text in ("🏠 Меню", "/start", "start", "/menu"):
            SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
            await tg_send_message(chat_id, WELCOME, reply_markup=menu_keyboard())
            return

        if text == "🔄 Очистить контекст":
            SESSIONS[chat_id] = {"mode": mode, "parts": {"link": None, "meta": ""}}
            await tg_send_message(chat_id, "Контекст очищен. Продолжайте.", reply_markup=menu_keyboard())
            return

        if text == "♻️ Перезапуск":
            SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
            await tg_send_message(chat_id, "Сессия перезапущена. Нажмите «📚 Оформить источник внутри текста».", reply_markup=menu_keyboard())
            return

        if text == "🛠 Починить сбои":
            await tg_send_message(chat_id, "Если долго нет ответа, просто повторите запрос.\nМы запускаем обработку в фоне, чтобы Telegram всегда получал 200.", reply_markup=menu_keyboard())
            return

        if text == "📚 Оформить источник внутри текста":
            SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}
            await tg_send_message(chat_id, "Режим оформления включён. Пришлите источник (название/журнал/год/том/стр/DOI) и гиперссылку. Можно по очереди.", reply_markup=menu_keyboard())
            return

        # режимы
        if mode == "format_citation":
            await handle_formatter_message(chat_id, text)
            return

        # дефолт — подсказать меню
        await tg_send_message(chat_id, "Выберите действие:", reply_markup=menu_keyboard())

    except Exception:
        logger.exception("process_update fatal error")

# ------------------------
# Webhook: мгновенный 200 + фон
# ------------------------
@app.post("/webhook/{path_secret}")
async def telegram_webhook(path_secret: str, request: Request, background_tasks: BackgroundTasks):
    if path_secret != WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    try:
        update = await request.json()
    except Exception:
        logger.exception("Bad Telegram update payload")
        return JSONResponse({"ok": True})  # отвечаем 200, чтобы не копить 503

    # ставим в фон, чтобы сразу вернуть 200
    background_tasks.add_task(process_update, update)
    return JSONResponse({"ok": True})

# GET на вебхук — для ручной проверки в браузере
@app.get("/webhook/{path_secret}")
async def webhook_get(path_secret: str):
    return JSONResponse({"ok": path_secret == WEBHOOK_SECRET})

# ------------------------
# Health / Root
# ------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "edsource-bot",
        "model": Z_AI_MODEL,
        "concurrency_limit": ZAI_CONCURRENCY_LIMIT,
    }

@app.get("/healthz")
def healthz():
    return {"ok": True, "status": "up"}

# ------------------------
# Lifecycle
# ------------------------
@app.on_event("startup")
async def on_startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=None)
    logger.info("HTTP client ready.")

@app.on_event("shutdown")
async def on_shutdown():
    global http_client
    if http_client:
        try:
            await http_client.aclose()
        except Exception:
            pass
    logger.info("HTTP client closed.")
