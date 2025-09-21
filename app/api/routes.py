from fastapi import APIRouter, Request, BackgroundTasks
from ..bot.handlers import process_update
from ..config.settings import WEBHOOK_SECRET, Z_AI_MODEL, ZAI_CONCURRENCY_LIMIT

router = APIRouter()

@router.post("/webhook/{path_secret}")
async def telegram_webhook(path_secret: str, request: Request, background_tasks: BackgroundTasks):
    if path_secret != WEBHOOK_SECRET:
        return {"ok": False, "error": "forbidden"}
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}
    background_tasks.add_task(process_update, update)
    return {"ok": True}

@router.get("/webhook/{path_secret}")
async def webhook_get(path_secret: str):
    return {"ok": path_secret == WEBHOOK_SECRET}

@router.get("/")
def root():
    return {
        "ok": True,
        "service": "edsource-bot",
        "model": Z_AI_MODEL,
        "concurrency_limit": ZAI_CONCURRENCY_LIMIT,
    }

@router.get("/healthz")
def healthz():
    return {"ok": True, "status": "up"}

