from typing import Optional, Dict, Any
from . import http_client
from ..config.settings import TELEGRAM_API_BASE, logger


def _is_expected_edit_error(desc: str) -> bool:
    """Ошибки редактирования, которые считаем штатными и не логируем как ERROR."""
    d = (desc or "").lower()
    return any(s in d for s in (
        "message can't be edited",
        "message is not modified",
        "message to edit not found",
    ))


async def tg_call(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Универсальный вызов Telegram Bot API.
    Возвращает JSON-ответ ТГ (даже когда ok=false), чтобы вызывающий код мог разобраться,
    и не спамит ERROR для ожидаемых кейсов редактирования.
    """
    if http_client.client is None:
        logger.error("HTTP client is not initialized")
        return None

    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        r = await http_client.client.post(url, json=payload, timeout=15)

        # Попытка разобрать JSON
        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "description": (r.text or "")}

        # Логирование
        if r.status_code >= 400 or not data.get("ok", True):
            desc = (data.get("description") or r.text or "").strip()
            # Для ожидаемых 400 при редактировании — INFO, остальное — ERROR
            log = logger.info if _is_expected_edit_error(desc) else logger.error
            log(f"Telegram {method} error {r.status_code}: {desc}")

        return data
    except Exception as e:
        logger.exception(f"Telegram {method} request failed: {e}")
        return None


# --------------- Высокоуровневые хелперы -----------------

async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Отправка нового сообщения. Возвращает JSON Telegram (ok/description/result).
    """
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    return await tg_call("sendMessage", payload)


async def tg_edit_message(chat_id: Optional[int], message_id: Optional[int], text: str,
                          reply_markup: Optional[Dict[str, Any]] = None,
                          inline_message_id: Optional[str] = None) -> bool:
    """
    Безопасное редактирование текста сообщения.
    Возвращает True, если:
      - Telegram ответил ok=true, или
      - текст не изменился (message is not modified).
    Возвращает False, если сообщение нельзя редактировать/не найдено и т.п.
    """
    payload: Dict[str, Any] = {
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    if inline_message_id:
        payload["inline_message_id"] = inline_message_id
    else:
        payload["chat_id"] = chat_id
        payload["message_id"] = message_id

    resp = await tg_call("editMessageText", payload)
    if not resp:
        return False

    if resp.get("ok"):
        return True

    desc = (resp.get("description") or "").lower()
    if "message is not modified" in desc:
        return True
    if "message can't be edited" in desc or "message to edit not found" in desc:
        return False

    return False


async def tg_edit_reply_markup(chat_id: Optional[int], message_id: Optional[int],
                               reply_markup: Optional[Dict[str, Any]] = None,
                               inline_message_id: Optional[str] = None) -> bool:
    """Безопасное обновление только клавиатуры у сообщения."""
    payload: Dict[str, Any] = {}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    if inline_message_id:
        payload["inline_message_id"] = inline_message_id
    else:
        payload["chat_id"] = chat_id
        payload["message_id"] = message_id

    resp = await tg_call("editMessageReplyMarkup", payload)
    if not resp:
        return False

    if resp.get("ok"):
        return True

    desc = (resp.get("description") or "").lower()
    if "message is not modified" in desc:
        return True
    if "message can't be edited" in desc or "message to edit not found" in desc:
        return False

    return False


async def tg_edit_or_send(chat_id: int, message_id: Optional[int], text: str,
                          reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Попробовать отредактировать сообщение; если нельзя — отправить новое.
    """
    ok = await tg_edit_message(chat_id, message_id, text, reply_markup=reply_markup)
    if ok:
        return {"ok": True}
    return (await tg_send_message(chat_id, text, reply_markup=reply_markup)) or {"ok": False}


async def tg_send_chat_action(chat_id: int, action: str = "typing") -> Optional[Dict[str, Any]]:
    """Показ «печатает…» и пр. Возможные action: typing, upload_document, upload_photo, …"""
    return await tg_call("sendChatAction", {"chat_id": chat_id, "action": action})


# --- Совместимость со старым кодом ---
async def tg_send_action(chat_id: int, action: str = "typing"):
    """Алиас для tg_send_chat_action, чтобы не падали старые импорты."""
    return await tg_send_chat_action(chat_id, action)
