from typing import Any, Dict
from .sessions import ensure_session
from .rate_limit import allow
from .commands import cmd_start, cmd_menu, cmd_clear, cmd_restart, cmd_fix
from .modes import format_citation
from .callbacks import handle_callback

async def route_update(update: Dict[str, Any]) -> None:
    """
    Единственная точка входа для логики бота:
    - разбираем message vs callback
    - применяем антиспам
    - роутим по режимам и командам
    """
    # 1) callback_query
    cb = update.get("callback_query")
    if cb:
        message = cb.get("message")
        if not message:
            return
        chat_id = message["chat"]["id"]
        data = (cb.get("data") or "").strip()
        await handle_callback(chat_id, data)
        return

    # 2) обычное сообщение
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    # антиспам
    if not allow(chat_id):
        return

    # системные команды
    if text in ("🏠 Меню", "/start", "start", "/menu"):
        await cmd_start(chat_id); return
    if text == "🔄 Очистить контекст":
        await cmd_clear(chat_id); return
    if text == "♻️ Перезапуск":
        await cmd_restart(chat_id); return
    if text == "🛠 Починить сбои":
        await cmd_fix(chat_id); return
    if text == "📚 Оформить источник внутри текста":
        await format_citation.enter_mode(chat_id); return

    # режимы
    sess = ensure_session(chat_id)
    mode = sess.get("mode", "menu")
    if mode == "format_citation":
        await format_citation.handle_message(chat_id, text)
        return

    # дефолт
    await cmd_menu(chat_id)
