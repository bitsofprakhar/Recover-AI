"""In-process asyncio scheduler (Phase 11 default runner).

One lightweight loop started with the FastAPI app: every tick it makes sure the
recurring jobs exist and executes everything due, through the same
`run_due_jobs` abstraction the synchronous API uses. This is deliberately
minimal - a single-instance asyncio task over the durable `background_jobs`
table. Redis + Celery would replace only this runner (a worker consuming the
same rows or the same registry), not the workflow. Disable it by setting
SCHEDULER_ENABLED=false.
"""
import asyncio
import logging

from sqlalchemy.orm import Session

from config import settings

logger = logging.getLogger("recoverai.scheduler")


def scheduler_tick(db: Session) -> dict:
    """One scheduler pass: ensure recurring jobs, then run everything due."""
    from services.jobs import ensure_recurring_jobs, run_due_jobs

    ensure_recurring_jobs(db)
    db.commit()
    return run_due_jobs(db)


async def scheduler_loop(stop: asyncio.Event, session_factory) -> None:
    interval = settings.scheduler_interval_seconds
    logger.info("scheduler started (interval=%ss)", interval)
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            try:
                db = session_factory()
                try:
                    scheduler_tick(db)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 - the loop must survive any tick failure
                logger.exception("scheduler tick failed")
    finally:
        logger.info("scheduler stopped")
