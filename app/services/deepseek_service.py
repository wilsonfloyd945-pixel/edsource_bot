import asyncio
from typing import Dict, List
from . import http_client
from ..config.settings import (
    DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_URL,
    ZAI_CONCURRENCY_LIMIT, logger
)

from .llm_gate import semaphore  # новый импорт

# используем тот же семафор, что и для Z.AI (можешь завести отдельный, если надо)
deepseek_semaphore = asyncio.Semaphore(ZAI_CONCURRENCY_LIMIT)

async def call_deepseek(messages: List[Dict[str, str]]) -> str:
    """
    messages — список [{"role":"system","content":"..."}, {"role":"user","content":"..."}]
    Возвращает строку с ответом модели.
    """
    if http_client.client is None:
        return "Сервис временно недоступен (HTTP клиент не инициализировался)."

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.0,   # для стабильности
        "stream": False,
    }

    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        async with semaphore:
            try:
                r = await http_client.client.post(DEEPSEEK_URL, headers=headers, json=data, timeout=25)
                if r.status_code in (429, 502, 503, 504):
                    logger.warning(f"DeepSeek transient {r.status_code}: {r.text[:200]}")
                    if attempt < max_attempts:
                        await asyncio.sleep(1.2 * attempt)
                        continue
                r.raise_for_status()
                payload = r.json()
                # OpenAI-совместимый формат
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
                logger.exception("DeepSeek error")
                if attempt < max_attempts:
                    await asyncio.sleep(1.0 * attempt)
                    continue
                return "Сервис перегружен. Попробуйте ещё раз позже."
