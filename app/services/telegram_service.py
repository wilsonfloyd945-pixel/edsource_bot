from typing import Optional, Dict, Any
from . import http_client
from ..config.settings import TELEGRAM_API_BASE, logger

async def tg_call(method: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if http_client.client is None:
        logger.error("HTTP client is not initialized")
        return None
    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        r = await http_client.client.post(url, json=payload, timeout=15)
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
