"""Постановка фоновых задач из API-обработчиков.

Отдельный модуль, чтобы и products, и sync ставили задачи одинаково — и чтобы
авто-триггеры (после блокировки, смены формулы, буфера) переиспользовали код,
а не дублировали логику enqueue.
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SyncJob

# kind → функция воркера. Единый справочник для API и планировщика.
KINDS = {
    "catalog": "sync_catalog",
    "stocks": "sync_stocks",
    "push": "push_marketplaces",
    "cards_update": "update_cards",
    "auto_cards": "auto_cards",
    "cards": "create_cards",
}


async def enqueue_kind(
    session: AsyncSession, supplier_id: int, kind: str, *, skip_if_running: bool = True
) -> SyncJob | None:
    """Поставить задачу kind для поставщика. Возвращает job или None, если пропущена.

    skip_if_running: не ставим второй экземпляр, если такой уже в очереди/выполняется —
    важно для авто-триггеров, которые могут сработать пачкой (например, массовая
    блокировка нескольких товаров подряд не должна плодить десяток пушей).
    """
    if skip_if_running:
        running = await session.scalar(
            select(func.count())
            .select_from(SyncJob)
            .where(
                SyncJob.supplier_id == supplier_id,
                SyncJob.kind == kind,
                SyncJob.status.in_(("queued", "running")),
            )
        )
        if running:
            return None

    job = SyncJob(supplier_id=supplier_id, kind=kind, status="queued")
    session.add(job)
    await session.commit()
    await session.refresh(job)

    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await redis.enqueue_job(KINDS[kind], supplier_id=supplier_id, job_id=job.id)
    finally:
        await redis.aclose()

    return job
