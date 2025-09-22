from typing import Optional, Dict, Any
from . import http_client
from ..config.settings import TELEGRAM_API_BASE, logger

async def tg_call(method: str, payload: Dict[str, Any], suppress_cant_edit: bool = False) -> Optional[Dict[str, Any]]:
    """
    Универсальный вызов Telegram API.
    Если suppress_cant_edit=True, предупреждение 'message can't be edited' не пишем как ERROR.
    """
    if client is None:
        logger.error("HTTP client is not initialized")
        return None

    url = f"{TELEGRAM_API_BASE}/{method}"

    try:
        r = await client.post(url, json=payload, timeout=15)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

        if r.status_code >= 400 or not data.get("ok", False):
            desc = (data.get("description") or r.text or "").lower()

            # Специальный кейс: нельзя редактировать (старое/удалённое/не-текстовое сообщение и т.п.)
            if suppress_cant_edit and "can't be edited" in desc:
                logger.info(f"Telegram {method} skipped: {data}")
                return None

            # Остальные ошибки — понизим до warning, чтобы не мусорить ERROR'ами
            logger.warning(f"Telegram {method} error {r.status_code}: {r.text}")
            return None

        return data

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
    """
    Пытаемся отредактировать плейсхолдер.
    Если Telegram вернёт 400 'message can't be edited' (или не удастся),
    просто отправляем новое сообщение и возвращаем False.
    """
    # Пакет для редактирования
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    # Пробуем редактировать
    resp = await tg_call("editMessageText", payload, suppress_cant_edit=True)

    # Если редактирование удалось — ок
    if resp and resp.get("ok"):
        return True

    # Иначе — отправляем новое сообщение, чтобы пользователь всё равно получил результат
    await tg_send_message(chat_id, text)
    return False

async def tg_send_action(chat_id: int, action: str = "typing") -> None:
    await tg_call("sendChatAction", {"chat_id": chat_id, "action": action})
