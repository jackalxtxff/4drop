"""Планировщик фоновых обновлений.

Раз в минуту смотрит расписание каждого поставщика и ставит задачи, которым
подошёл срок. Срок считается от времени ПОСЛЕДНЕГО ЗАПУСКА, а не по фиксированной
сетке: если выгрузка каталога заняла 40 минут, следующая не должна стартовать
сразу же только потому, что «наступил час».
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Credential, Platform, Supplier, SyncJob, SyncSettings

log = logging.getLogger(__name__)

# kind задачи → имя функции воркера
KINDS = {
    "catalog": "sync_catalog",
    "stocks": "sync_stocks",
    "push": "push_marketplaces",
    "orders": "sync_orders",
    "cards_update": "update_cards",
    "auto_cards": "auto_cards",
}


async def _due(session, supplier_id: int, kind: str, interval_minutes: int) -> bool:
    """Пора ли запускать. Интервал 0 = задача выключена."""
    if interval_minutes <= 0:
        return False

    # Уже в работе — второй запуск не ставим, иначе задачи наложатся и будут
    # дублировать запросы к 4tochki.
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
        return False

    last = await session.scalar(
        select(func.max(SyncJob.started_at)).where(
            SyncJob.supplier_id == supplier_id, SyncJob.kind == kind
        )
    )
    if last is None:
        return True

    return datetime.now(UTC) - last >= timedelta(minutes=interval_minutes)


async def schedule_due(ctx: dict) -> None:
    """Cron: раз в минуту ставит задачи, которым подошёл срок."""
    redis = ctx["redis"]

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Supplier, SyncSettings)
                .join(SyncSettings, SyncSettings.supplier_id == Supplier.id)
                .where(Supplier.is_active.is_(True))
            )
        ).all()

        # Поставщики без доступов к 4tochki синхронизировать нечем — пропускаем их,
        # чтобы планировщик не плодил падающие «доступы не заданы» задачи каждую
        # минуту. Ручной запуск такому поставщику всё равно покажет ошибку.
        configured = set(
            (
                await session.execute(
                    select(Credential.supplier_id).where(
                        Credential.platform == Platform.FOURTOCHKI,
                        Credential.secrets_encrypted.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )

        for supplier, settings in rows:
            if supplier.id not in configured:
                continue

            intervals = {
                "catalog": settings.catalog_interval_minutes,
                "stocks": settings.stocks_interval_minutes,
                "push": settings.push_interval_minutes,
                # Заказы: вебхуков по ним у WB нет, поэтому опрашиваем часто —
                # пропущенный заказ FBS это сорванный дедлайн сборки.
                "orders": settings.orders_interval_minutes,
                "cards_update": settings.cards_update_interval_minutes,
                # Авто-создание идёт по расписанию только при включённом авто-режиме.
                "auto_cards": (
                    settings.auto_cards_interval_minutes if settings.auto_mode else 0
                ),
            }

            for kind, minutes in intervals.items():
                if not await _due(session, supplier.id, kind, minutes):
                    continue

                job = SyncJob(supplier_id=supplier.id, kind=kind, status="queued")
                session.add(job)
                await session.commit()
                await session.refresh(job)

                await redis.enqueue_job(
                    KINDS[kind], supplier_id=supplier.id, job_id=job.id
                )
                log.info(
                    "Запланировано: %s для поставщика %s (интервал %s мин)",
                    kind, supplier.id, minutes,
                )
