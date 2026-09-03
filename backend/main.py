import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import cases, demo, events, jobs, metrics, webhooks
from config import settings
from db import SessionLocal
from services.scheduler import scheduler_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop: asyncio.Event | None = None
    task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler_loop(stop, SessionLocal))
    yield
    if task is not None and stop is not None:
        stop.set()
        await task


app = FastAPI(title="RecoverAI Backend", version="0.1.0", lifespan=lifespan)

app.include_router(webhooks.router)
app.include_router(events.router)
app.include_router(cases.router)
app.include_router(metrics.router)
app.include_router(jobs.router)
app.include_router(demo.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "scheduler_enabled": settings.scheduler_enabled}
