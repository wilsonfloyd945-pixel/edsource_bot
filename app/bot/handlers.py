from typing import Dict, Any
from .router import route_update

async def process_update(update: Dict[str, Any]) -> None:
    try:
        await route_update(update)
    except Exception:
        # не падаем на одном апдейте
        return
