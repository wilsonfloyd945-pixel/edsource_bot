import asyncio
from typing import Dict
from ..config.settings import PER_CHAT_COOLDOWN

LAST_USED_AT: Dict[int, float] = {}

def allow(chat_id: int) -> bool:
    """
    Возвращает True, если можно обрабатывать сообщение (кулдаун прошёл).
    """
    now = asyncio.get_event_loop().time()
    last = LAST_USED_AT.get(chat_id, 0.0)
    if now - last < PER_CHAT_COOLDOWN:
        return False
    LAST_USED_AT[chat_id] = now
    return True
