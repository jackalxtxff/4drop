"""Создание карточек товаров на маркетплейсах.

Реализован Wildberries (Content API). Ozon — следующий шаг.

Важно про модерацию: приём карточки WB (HTTP 200, error=false) означает только
постановку в очередь, а не публикацию. Поэтому статус ставим «на модерации», а
nmID/chrtID и ошибки забираем отдельным проходом — карточка появляется в списке
кабинета с задержкой.

Ядро создания вынесено в _create_wb_cards: им пользуются и ручная интеграция
(create_cards), и авто-режим (auto_cards в cards_auto.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.formula import FormulaError, compile_formula, evaluate
from app.integrations.wb.cards import (
    CardBuildError,
    barcode_for,
    build_card,
    card_content_hash,
    is_ours,
    vendor_code,
)
from app.integrations.wb.client import WBClient
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
from app.tasks.catalog_sync import get_or_create_settings

log = logging.getLogger(__name__)

# Пауза перед тем, как спрашивать WB о результате: карточка появляется в списке
# кабинета не мгновенно.
SETTLE_SECONDS = 10


async def _link(
    session: AsyncSession,
    supplier_id: int,
    product_id: int,
    platform: str,
) -> ProductLink:
    existing = (
        await session.execute(
            select(ProductLink).where(
                ProductLink.product_id == product_id, ProductLink.platform == platform
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    link = ProductLink(supplier_id=supplier_id, product_id=product_id, platform=platform)
    session.add(link)
    return link


async def _wb_credential(session: AsyncSession, supplier_id: int) -> Credential | None:
    return (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.WB,
            )
        )
    ).scalar_one_or_none()


async def create_wb_cards(
    session: AsyncSession,
    supplier_id: int,
    products: list[Product],
    api_key: str,
    settings: SyncSettings,
    job: SyncJob,
) -> tuple[str, str]:
    """Ядро создания карточек WB. Возвращает (level, message).

    Заблокированные товары сюда попадать не должны — их отсекают вызывающие.
    """
    try:
        price_formula = compile_formula(settings.wb_price_formula)
    except FormulaError as exc:
        return "error", f"Формула цены WB некорректна: {exc}"

    client = WBClient(api_key)

    cards: list[dict] = []
    by_vendor: dict[str, Product] = {}
    skipped = 0

    for product in products:
        price = (
            evaluate(price_formula, product.min_price, product.price_rozn, product.weight)
            if product.min_price
            else None
        )
        try:
            cards.append(build_card(product, price))
        except CardBuildError as exc:
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.status = IntegrationStatus.ERROR
            link.status_message = str(exc)
            product.integration_status = IntegrationStatus.ERROR
            skipped += 1
            continue
        by_vendor[vendor_code(product)] = product

    await session.commit()

    if not cards:
        return "error", f"Ни один товар не подошёл под карточку WB (пропущено: {skipped})"

    sent, upload_errors = await client.upload_cards(cards)

    for product in by_vendor.values():
        link = await _link(session, supplier_id, product.id, Platform.WB)
        link.status = IntegrationStatus.PENDING
        link.status_message = "Отправлено на модерацию WB"
        link.barcode = barcode_for(product)
        product.integration_status = IntegrationStatus.PENDING
    job.processed = sent
    await session.commit()

    await asyncio.sleep(SETTLE_SECONDS)

    # cards_map возвращает карточки кабинета по vendorCode; берём только наши (префикс 4D-).
    created = {vc: c for vc, c in (await client.cards_map()).items() if is_ours(vc)}
    errors = await client.card_errors()

    ok = 0
    for vc, product in by_vendor.items():
        link = await _link(session, supplier_id, product.id, Platform.WB)

        if vc in errors:
            link.status = IntegrationStatus.ERROR
            link.status_message = errors[vc]
            product.integration_status = IntegrationStatus.ERROR
            continue

        card = created.get(vc)
        if card:
            link.nm_id = card["nm_id"]
            link.chrt_id = card["chrt_id"]
            link.barcode = card["barcode"] or link.barcode
            link.status = IntegrationStatus.ACTIVE
            link.status_message = None
            link.card_hash = card_content_hash(product)
            product.integration_status = IntegrationStatus.ACTIVE
            ok += 1
        else:
            link.status = IntegrationStatus.PENDING
            link.status_message = "Ожидает обработки на стороне WB"
            product.integration_status = IntegrationStatus.PENDING

    job.processed = ok
    job.failed = skipped + len(errors)

    env = "песочница WB" if client.sandbox else "боевой кабинет WB"
    parts = [f"[{env}] Создано карточек: {ok} из {len(cards)}"]
    if errors:
        parts.append(f"отклонено WB: {len(errors)}")
    if skipped:
        parts.append(f"пропущено: {skipped}")
    if upload_errors:
        parts.append("ошибки отправки: " + "; ".join(upload_errors[:2]))

    level = "error" if (errors or upload_errors) and ok == 0 else "info"
    return level, ". ".join(parts)


async def create_cards(ctx: dict, supplier_id: int, job_id: int) -> None:
    """Ручная интеграция: карточки по явно выбранным товарам (из products/integrate)."""
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        job.status = "running"
        await session.commit()

        payload = job.payload or {}
        product_ids: list[int] = payload.get("product_ids") or []
        platforms: list[str] = payload.get("platforms") or []

        if "wb" not in platforms:
            job.status = "failed"
            job.message = "Ozon пока не поддерживается — создание карточек есть только для WB."
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        cred = await _wb_credential(session, supplier_id)
        if cred is None or not cred.secrets_encrypted:
            job.status = "failed"
            job.message = "Доступы к Wildberries не заданы"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        api_key = json.loads(decrypt_secret(cred.secrets_encrypted))["api_key"]

        # Заблокированные исключаем даже при ручном запуске: блокировка — это «никогда
        # не трогать на маркетплейсе», сильнее ручного выбора.
        products = list(
            (
                await session.execute(
                    select(Product).where(
                        Product.supplier_id == supplier_id,
                        Product.id.in_(product_ids),
                        Product.sync_blocked.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        blocked = len(product_ids) - len(products)

        if not products:
            job.status = "failed"
            job.message = (
                "Все выбранные товары заблокированы для синхронизации"
                if blocked
                else "Товары не найдены"
            )
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        settings = await get_or_create_settings(session, supplier_id)

        try:
            level, message = await create_wb_cards(
                session, supplier_id, products, api_key, settings, job
            )
            if blocked:
                message += f". Заблокировано, пропущено: {blocked}"
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
