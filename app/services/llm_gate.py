import asyncio
from ..config.settings import LLM_CONCURRENCY_LIMIT

# Один общий семафор для всех вызовов LLM (DeepSeek, Z.AI, и т.п.)
semaphore = asyncio.Semaphore(LLM_CONCURRENCY_LIMIT)
