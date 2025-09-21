import asyncio
from typing import Dict, List
from .http_client import client
from ..config.settings import (
    Z_AI_API_KEY, Z_AI_MODEL, ZAI_URL,
    ZAI_CONCURRENCY_LIMIT, logger
)

zai_semaphore = asyncio.Semaphore(ZAI_CONCURRENCY_LIMIT)

async def call_zai(messages: List[Dict[str, str]]) -> str:
    if client is None:
        return "Сервис временно недоступен (HTTP клиент не инициализировался)."
    headers = {
        "Authorization": f"Bearer {Z_AI_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": Z_AI_MODEL,
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
    }

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        async with zai_semaphore:
            try:
                r = await client.post(ZAI_URL, headers=headers, json=data, timeout=25)
                if r.status_code in (429, 502, 503, 504):
                    logger.warning(f"Z.AI transient {r.status_code}: {r.text[:200]}")
                    if attempt < max_attempts:
                        await asyncio.sleep(1.2 * attempt)
                        continue
                r.raise_for_status()
                payload = r.json()
                reply = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return (reply or "").strip() or "Извините, модель вернула пустой ответ."
            except asyncio.TimeoutError:
                if attempt < max_attempts:
                    await asyncio.sleep(1.2 * attempt)
                    continue
                return "Сервис отвечает дольше обычного. Попробуйте ещё раз."
            except Exception:
                # httpx ошибки и т.п.
                logger.exception("Z.AI error")
                if attempt < max_attempts:
                    await asyncio.sleep(1.0 * attempt)
                    continue
                return "Сервис перегружен. Попробуйте ещё раз позже."

async def call_llm(messages: List[Dict[str, str]]) -> str:
    return await call_zai(messages)
