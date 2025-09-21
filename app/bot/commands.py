from typing import Dict, Any
from .sessions import SESSIONS
from .ui import WELCOME, menu_keyboard
from ..services.telegram_service import tg_send_message

async def cmd_start(chat_id: int) -> None:
    SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
    await tg_send_message(chat_id, WELCOME, reply_markup=menu_keyboard())

async def cmd_menu(chat_id: int) -> None:
    await cmd_start(chat_id)

async def cmd_clear(chat_id: int) -> None:
    mode = SESSIONS.get(chat_id, {}).get("mode", "menu")
    SESSIONS[chat_id] = {"mode": mode, "parts": {"link": None, "meta": ""}}
    await tg_send_message(chat_id, "Контекст очищен. Продолжайте.", reply_markup=menu_keyboard())

async def cmd_restart(chat_id: int) -> None:
    SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
    await tg_send_message(chat_id, "Сессия перезапущена. Нажмите «📚 Оформить источник внутри текста».", reply_markup=menu_keyboard())

async def cmd_fix(chat_id: int) -> None:
    await tg_send_message(chat_id, "Если долго нет ответа, просто повторите запрос.\nМы запускаем обработку в фоне, чтобы Telegram всегда получал 200.", reply_markup=menu_keyboard())
