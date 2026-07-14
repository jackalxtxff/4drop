"""Воркер фоновых задач (arq)."""

import logging
from datetime import UTC, datetime

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import SessionLocal
from app.models import SyncJob
from app.tasks.catalog_sync import sync_catalog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def create_cards(ctx: dict, supplier_id: int, job_id: int) -> None:
    """Заглушка следующего шага: создание карточек на WB и Ozon.

    Задача уже ставится из UI и её прогресс виден, но сами карточки пока не создаются —
    для этого нужны клиенты Content API (WB) и Product API (Ozon).
    """
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return
        job.status = "failed"
        job.message = (
            "Создание карточек ещё не реализовано: следующий шаг — "
            "WB Content API и Ozon Product API."
        )
        job.finished_at = datetime.now(UTC)
        await session.commit()


class WorkerSettings:
    functions = [sync_catalog, create_cards]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 3600  # выгрузка каталога на десятки тысяч позиций идёт долго
    keep_result = 3600
