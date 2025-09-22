from typing import Optional, Dict, Any
import asyncio
from .http_client import client
from ..config.settings import TELEGRAM_API_BASE, logger

async def tg_call(method: str, payload: Dict[str, Any], *, 
                  suppress_cant_edit: bool = False, 
                  max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """
    Универсальный вызов Telegram API с повторными попытками.
    """
    if client is None or client.closed:
        logger.error("HTTP client is not initialized or closed")
        return None

    url = f"{TELEGRAM_API_BASE}/{method}"
    
    for attempt in range(max_retries):
        try:
            r = await client.post(url, json=payload, timeout=15)
            ctype = r.headers.get("content-type", "")
            data = r.json() if ctype.startswith("application/json") else {}

            ok = bool(data.get("ok", False))
            if not ok:
                desc = (data.get("description") or r.text or "").lower()
                # Спец-кейс: невозможность редактирования — не шумим
                if suppress_cant_edit and "can't be edited" in desc:
                    logger.info(f"Telegram {method} skipped (can't edit): {data}")
                    return None

                logger.warning(
                    "Telegram %s failed: status=%s, description=%s, payload_keys=%s",
                    method, r.status_code, data.get("description"), list(payload.keys())
                )
                return None

            return data

        except Exception as e:
            if attempt == max_retries - 1:
                logger.exception(f"Telegram call failed after {max_retries} attempts: {method}")
                return None
            
            # Экспоненциальная задержка перед повторной попыткой
            delay = 2 ** attempt
            logger.warning(f"Telegram call attempt {attempt + 1} failed: {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)

async def tg_send_message(chat_id: int, text: str,
                          reply_markup: Optional[Dict[str, Any]] = None,
                          max_retries: int = 3) -> Optional[int]:
    """
    Отправка сообщения с повторными попытками и проверкой состояния клиента.
    """
    # Проверяем состояние клиента перед отправкой
    if client is None or client.closed:
        logger.error("HTTP client is not initialized or closed, cannot send message")
        return None

    txt = (text or "").strip()
    logger.info("tg_send_message: chat_id=%s, len=%s", chat_id, len(txt))

    # Первая попытка — с HTML и разметкой
    for attempt in range(max_retries):
        resp = await tg_call("sendMessage", {
            "chat_id": chat_id,
            "text": txt,
            "reply_markup": reply_markup,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, max_retries=1)  # Не повторяем внутри tg_call, так как повторяем здесь
        
        if resp and resp.get("result") and resp["result"].get("message_id"):
            return resp["result"]["message_id"]
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1)  # Короткая задержка перед повторной попыткой

    # Вторая попытка — без HTML и без разметки
    logger.info("tg_send_message: retry without HTML/markup")
    for attempt in range(max_retries):
        resp2 = await tg_call("sendMessage", {
            "chat_id": chat_id,
            "text": txt,
            "disable_web_page_preview": True,
        }, max_retries=1)
        
        if resp2 and resp2.get("result") and resp2["result"].get("message_id"):
            return resp2["result"]["message_id"]
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1)

    logger.warning("tg_send_message: failed to deliver to chat_id=%s after %s attempts", 
                  chat_id, max_retries * 2)
    return None

async def tg_edit_message(chat_id: int, message_id: int, text: str, max_retries: int = 2) -> bool:
    """
    Редактирование сообщения с повторными попытками.
    """
    if client is None or client.closed:
        logger.error("HTTP client is not initialized or closed, cannot edit message")
        return False

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": (text or "").strip(),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    
    for attempt in range(max_retries):
        resp = await tg_call("editMessageText", payload, suppress_cant_edit=True, max_retries=1)
        if resp and resp.get("ok"):
            return True
        
        if attempt < max_retries - 1:
            await asyncio.sleep(1)
    
    # Фолбэк — отправим как новое
    await tg_send_message(chat_id, text)
    return False

async def tg_send_action(chat_id: int, action: str = "typing", max_retries: int = 2) -> None:
    """
    Отправка действия с повторными попытками.
    """
    if client is None or client.closed:
        logger.error("HTTP client is not initialized or closed, cannot send action")
        return

    for attempt in range(max_retries):
        resp = await tg_call("sendChatAction", {"chat_id": chat_id, "action": action}, max_retries=1)
        if resp:
            return
        
        if attempt < max_retries - 1:
            await asyncio.sleep(0.5)

# Добавим функцию для проверки состояния клиента
def is_telegram_client_ready() -> bool:
    """Проверяет, инициализирован ли и готов ли HTTP клиент для Telegram."""
    return client is not None and not client.closed