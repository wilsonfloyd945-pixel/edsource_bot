from fastapi import FastAPI
from contextlib import asynccontextmanager
from .api.routes import router
from .services.http_client import init_http_client, close_http_client

@asynccontextmanager
async def lifespan(app: FastAPI):
    # код при запуске
    await init_http_client()
    yield
    # код при завершении
    await close_http_client()

def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return app


