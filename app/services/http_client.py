from typing import Optional
import httpx
from ..config.settings import logger

client: Optional[httpx.AsyncClient] = None

async def init_http_client():
    global client
    client = httpx.AsyncClient(timeout=None)
    logger.info("HTTP client ready.")

async def close_http_client():
    global client
    if client:
        try:
            await client.aclose()
        except Exception:
            pass
    logger.info("HTTP client closed.")
