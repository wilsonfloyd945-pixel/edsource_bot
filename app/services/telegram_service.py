from typing import Optional, Dict, Any
from . import http_client                      # <-- ВАЖНО: импортируем модуль, не переменную
from ..config.settings import TELEGRAM_API_BASE, logger

async def tg_call(method: str, payload: Dict[str, Any], *,
                  suppress_cant_edit: bool = False) -> Optional[Dict[str, Any]]:
    """
    Универсальный вызов Telegram API.
    Если suppress_cant_edit=True, 'message can't be edited' не логируем как ошибку.
    """
    if http_client.client is None:              # <-- ВСЕГДА обращаемся через модуль
        logger.error("HTTP client is not initialized")
        return None

    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        r = await http_client.client.post(url, json=payload, timeout=15)
        ctype = r.headers.get("content-type", "")
        data = r.json() if ctype.startswith("application/json") else {}

        ok = bool(data.get("ok", False))
        if not ok:
            desc = (data.get("description") or r.text or "").lower()
            if suppress_cant_edit and "can't be edited" in desc:
                logger.info(f"Telegram {method} skipped (can't edit): {data}")
                return None

            logger.warning(
                "Telegram %s failed: status=%s, description=%s, payload_keys=%s",
                method, r.status_code, data.get("description"), list(payload.keys())
            )
            return None

        return data

    except Exception:
        logger.exception(f"Telegram call failed: {method}")
        return None


async def tg_send_message(chat_id: int, text: str,
                          reply_markup: Optional[Dict[str, Any]] = None) -> Optional[int]:
    """
    Отправка в 2 попытки:
    1) HTML + reply_markup
    2) если провал — без parse_mode и без reply_markup
    """
    txt = (text or "").strip()
    logger.info("tg_send_message: chat_id=%s, len=%s", chat_id, len(txt))

    resp = await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": txt,
        "reply_markup": reply_markup,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    if resp and resp.get("result") and resp["result"].get("message_id"):
        return resp["result"]["message_id"]

    logger.info("tg_send_message: retry without HTML/markup")
    resp2 = await tg_call("sendMessage", {
        "chat_id": chat_id,
        "text": txt,
        "disable_web_page_preview": True,
    })
    if resp2 and resp2.get("result") and resp2["result"].get("message_id"):
        return resp2["result"]["message_id"]

    logger.warning("tg_send_message: failed to deliver to chat_id=%s", chat_id)
    return None


async def tg_edit_message(chat_id: int, message_id: int, text: str) -> bool:
    """
    Пытаемся отредактировать плейсхолдер.
    Если нельзя — шлём новое сообщение, без красных ошибок в логах.
    """
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": (text or "").strip(),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = await tg_call("editMessageText", payload, suppress_cant_edit=True)
    if resp and resp.get("ok"):
        return True

    # фолбэк — отправим как новое
    await tg_send_message(chat_id, text)
    return False
