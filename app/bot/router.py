from typing import Any, Dict
from .sessions import ensure_session, SESSIONS
from .rate_limit import allow
from .commands import cmd_start, cmd_menu, cmd_clear, cmd_restart, cmd_fix
from .modes import format_citation
from .callbacks import handle_callback
from .ui import model_keyboard
from ..services.telegram_service import tg_send_message, tg_edit_message


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

        # --- выбор модели ---
        if data.startswith("model:"):
            choice = data.split(":", 1)[1]
            sess = ensure_session(chat_id)

            if choice == "help":
                text = (
                    "• ⚡ LLaMA 3.1 8B (Amvera) — быстрее, хватает для форматирования.\n"
                    "• DeepSeek — быстрый, но результат может плавать.\n"
                    "• ZAI — как запасной провайдер.\n\n"
                    "Выбери модель:"
                )
                await tg_edit_message(chat_id, message["message_id"], text)
                await tg_send_message(chat_id, "Выбери модель:", reply_markup=model_keyboard())
                return

            # сохраняем выбор модели прямо в сессии
            sess["llm"] = choice  # 'amvera' | 'deepseek' | 'zai'
            SESSIONS[chat_id] = sess

            confirm = {
                "amvera": "✅ Выбрана LLaMA 3.1 8B (Amvera).",
                "deepseek": "✅ Выбран DeepSeek.",
                "zai": "✅ Выбран ZAI.",
            }.get(choice, f"✅ Модель: {choice}")

            await tg_edit_message(chat_id, message["message_id"], confirm)
            await format_citation.enter_mode(chat_id)
            return

        # остальные коллбеки
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
        await cmd_start(chat_id)
        return
    if text == "🔄 Очистить контекст":
        await cmd_clear(chat_id)
        return
    if text == "♻️ Перезапуск":
        await cmd_restart(chat_id)
        return
    if text == "🛠 Починить сбои":
        await cmd_fix(chat_id)
        return
    if text in ("/model", "Сменить модель", "Выбрать модель"):
        await tg_send_message(chat_id, "Выбери модель:", reply_markup=model_keyboard())
        return
    if text == "📚 Оформить источник внутри текста":
        await format_citation.enter_mode(chat_id)
        return

    # режимы
    sess = ensure_session(chat_id)
    mode = sess.get("mode", "menu")
    if mode == "format_citation":
        await format_citation.handle_message(chat_id, text)
        return

    # дефолт
    await cmd_menu(chat_id)
