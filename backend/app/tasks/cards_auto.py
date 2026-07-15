"""Авто-режим: создание карточек для товаров, появившихся в наличии.

Работает только при включённом SyncSettings.auto_mode. Находит товары, которые:
  * есть в наличии (total_rest > 0) — НЕ заливаем весь каталог с нулями;
  * ещё не заведены на WB (нет ProductLink) — или прошлая попытка была ошибкой;
  * не заблокированы вручную (sync_blocked);
  * годятся под карточку (шины/диски, есть бренд и цена — это проверит build_card).

Пуш цен/остатков по уже активным карточкам делает push_marketplaces отдельно.
За один прогон создаём не больше auto_cards_batch_limit карточек — чтобы не упереться
в rate limit WB и не завалить модерацию разом.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import (
    Credential,
    IntegrationStatus,
    LogEntry,
    Platform,
    Product,
    ProductLink,
    SyncJob,
    SyncSettings,
)
from app.security import decrypt_secret
from app.tasks.cards_sync import create_wb_cards
from app.tasks.catalog_sync import get_or_create_settings


async def _pick_new_in_stock(
    session: AsyncSession, supplier_id: int, limit: int
) -> list[Product]:
    """Товары в наличии, не заблокированные, ещё без активной/ожидающей карточки WB."""
    wb_link = (
        select(ProductLink.id)
        .where(
            ProductLink.product_id == Product.id,
            ProductLink.platform == Platform.WB,
            # active/pending — уже заведены или в очереди; error/none — можно (пере)создать.
            ProductLink.status.in_(
                (IntegrationStatus.ACTIVE, IntegrationStatus.PENDING)
            ),
        )
    )
    stmt = (
        select(Product)
        .where(
            Product.supplier_id == supplier_id,
            Product.total_rest > 0,
            Product.sync_blocked.is_(False),
            ~wb_link.exists(),
        )
        # Сначала товары с большим остатком — они важнее для продаж.
        .order_by(Product.total_rest.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def auto_cards(ctx: dict, supplier_id: int, job_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        job.status = "running"
        await session.commit()

        settings = await get_or_create_settings(session, supplier_id)
        if not settings.auto_mode:
            job.status = "done"
            job.message = "Авто-режим выключен"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        cred = (
            await session.execute(
                select(Credential).where(
                    Credential.supplier_id == supplier_id,
                    Credential.platform == Platform.WB,
                )
            )
        ).scalar_one_or_none()
        if cred is None or not cred.secrets_encrypted:
            job.status = "failed"
            job.message = "Доступы к Wildberries не заданы"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        api_key = json.loads(decrypt_secret(cred.secrets_encrypted))["api_key"]

        products = await _pick_new_in_stock(
            session, supplier_id, settings.auto_cards_batch_limit
        )
        job.total = len(products)
        await session.commit()

        if not products:
            job.status = "done"
            job.message = "Новых товаров в наличии для авто-создания нет"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        try:
            level, message = await create_wb_cards(
                session, supplier_id, products, api_key, settings, job
            )
            message = f"Авто-создание. {message}"
            job.status = "done" if level == "info" else "failed"
            job.message = message
            job.finished_at = datetime.now(UTC)
            session.add(
                LogEntry(
                    supplier_id=supplier_id,
                    job_id=job_id,
                    level=level,
                    platform=Platform.WB,
                    message=message,
                )
            )
            await session.commit()

        except Exception as exc:  # noqa: BLE001 — иначе задача зависнет в «running»
            await session.rollback()
            job = await session.get(SyncJob, job_id)
            if job:
                job.status = "failed"
                job.message = str(exc)[:500]
                job.finished_at = datetime.now(UTC)
                await session.commit()
            raise
