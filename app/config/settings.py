import os
import logging
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("uvicorn.error")

def env_required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

# обязательные
TELEGRAM_TOKEN = env_required("TELEGRAM_TOKEN")
Z_AI_API_KEY   = env_required("Z_AI_API_KEY")

# опциональные / с дефолтами
WEBHOOK_SECRET          = os.environ.get("WEBHOOK_SECRET", "amagh743").strip()
Z_AI_MODEL              = os.environ.get("Z_AI_MODEL", "glm-4.5-Flash").strip()
ZAI_CONCURRENCY_LIMIT   = int(os.environ.get("ZAI_CONCURRENCY_LIMIT", "2"))
PER_CHAT_COOLDOWN       = float(os.environ.get("PER_CHAT_COOLDOWN", "0.7"))
MODEL_WATCHDOG_SECONDS  = int(os.environ.get("MODEL_WATCHDOG_SECONDS", "25"))

# константы API
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"


# какой провайдер LLM используем: 'zai' (по умолчанию) или 'deepseek'
MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "zai").strip()

# DeepSeek (используется, если MODEL_PROVIDER=deepseek)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL   = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_URL     = "https://api.deepseek.com/v1/chat/completions"

LLM_CONCURRENCY_LIMIT = int(os.environ.get("LLM_CONCURRENCY_LIMIT", "1"))


