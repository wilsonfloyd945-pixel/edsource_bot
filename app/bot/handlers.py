import asyncio
from typing import Optional, Dict, Any
from .sessions import SESSIONS, LAST_USED_AT, ensure_session
from .formatting import LINK_RE, first_formatted_line
from .ui import SYSTEM_PROMPT_FORMATTER, menu_keyboard, WELCOME
from ..services.telegram_service import tg_send_message, tg_edit_message, tg_send_action
from ..services.zai_service import call_llm
from ..config.settings import PER_CHAT_COOLDOWN, MODEL_WATCHDOG_SECONDS

def fire_and_forget(coro):
    try:
        asyncio.create_task(coro)
    except RuntimeError:
        asyncio.get_event_loop().create_task(coro)

async def handle_formatter_message(chat_id: int, text: str) -> None:
    sess = ensure_session(chat_id)
    parts = sess["parts"]
    txt = (text or "").strip()

    urls = LINK_RE.findall(txt)
    if urls:
        if not parts.get("link"):
            parts["link"] = urls[0]
        meta_candidate = LINK_RE.sub("", txt).strip()
        if meta_candidate:
            if parts.get("meta"):
                parts["meta"] = (parts["meta"] + " " + meta_candidate).strip()
            else:
                parts["meta"] = meta_candidate
    else:
        if parts.get("meta"):
            parts["meta"] = (parts["meta"] + " " + txt).strip()
        else:
            parts["meta"] = txt

    if parts.get("link") and parts.get("meta"):
        await tg_send_action(chat_id, "typing")
        placeholder_id = await tg_send_message(chat_id, "Оформляю…", reply_markup=menu_keyboard())
        fire_and_forget(_format_worker(chat_id, parts.copy(), placeholder_id))
    else:
        if not parts.get("link"):
            await tg_send_message(chat_id, "Пришлите гиперссылку на источник (начинается с http/https).", reply_markup=menu_keyboard())
        elif not parts.get("meta"):
            await tg_send_message(chat_id, "Пришлите данные об источнике (название, журнал/место публикации, год, том/номер, страницы, DOI).", reply_markup=menu_keyboard())

async def _format_worker(chat_id: int, parts: Dict[str, Any], placeholder_id: Optional[int]) -> None:
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
        out = "Не удалось оформить источник. Попробуйте ещё раз."

    if placeholder_id:
        ok = await tg_edit_message(chat_id, placeholder_id, out)
        if not ok:
            await tg_send_message(chat_id, out, reply_markup=menu_keyboard())
    else:
        await tg_send_message(chat_id, out, reply_markup=menu_keyboard())

    SESSIONS[chat_id] = {"mode": "format_citation", "parts": {"link": None, "meta": ""}}

async def process_update(update: Dict[str, Any]) -> None:
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

        now = asyncio.get_event_loop().time()
        last = LAST_USED_AT.get(chat_id, 0.0)
        if now - last < PER_CHAT_COOLDOWN:
            return
        LAST_USED_AT[chat_id] = now

        sess = ensure_session(chat_id)
        mode = sess.get("mode", "menu")

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

        if mode == "format_citation":
            await handle_formatter_message(chat_id, text)
            return

        await tg_send_message(chat_id, "Выберите действие:", reply_markup=menu_keyboard())

    except Exception:
        return
