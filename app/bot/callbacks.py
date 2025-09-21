from .sessions import SESSIONS
from .ui import menu_keyboard
from ..services.telegram_service import tg_send_message

async def handle_callback(chat_id: int, data: str) -> None:
    if data == "menu":
        SESSIONS[chat_id] = {"mode": "menu", "parts": {"link": None, "meta": ""}}
        await tg_send_message(chat_id, "Меню:", reply_markup=menu_keyboard())
    # сюда легко добавлять новые callback-и по мере роста бота
