"""Воркер фоновых задач (arq)."""

import logging
from datetime import UTC, datetime

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.db import SessionLocal
from app.models import SyncJob
from app.tasks.cards_sync import create_cards
from app.tasks.catalog_sync import sync_catalog
from app.tasks.push_sync import push_marketplaces
from app.tasks.scheduler import schedule_due
from app.tasks.stocks_sync import sync_stocks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _fail(job_id: int, message: str) -> None:
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.message = message
        job.finished_at = datetime.now(UTC)
        await session.commit()


class WorkerSettings:
    functions = [sync_catalog, sync_stocks, create_cards, push_marketplaces]
    cron_jobs = [cron(schedule_due, minute=set(range(60)), run_at_startup=True)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 3600  # выгрузка каталога на десятки тысяч позиций идёт долго
    keep_result = 3600
