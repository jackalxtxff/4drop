"""Создание карточек товаров на маркетплейсах.

Реализован Wildberries (Content API). Ozon — следующий шаг.

Важно про модерацию: приём карточки WB (HTTP 200, error=false) означает только
постановку в очередь, а не публикацию. Поэтому статус ставим «на модерации», а
nmID/chrtID и ошибки забираем отдельным проходом — карточка появляется в списке
кабинета с задержкой.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.wb.cards import (
    CardBuildError,
    barcode_for,
    build_card,
    is_ours,
    vendor_code,
)
from app.integrations.wb.client import WBClient, WBError
from app.models import (
    Credential,
    IntegrationStatus,
    LogEntry,
    Platform,
    Product,
    ProductLink,
    SyncJob,
)
from app.formula import FormulaError, compile_formula, evaluate
from app.tasks.catalog_sync import get_or_create_settings
from app.security import decrypt_secret

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

    link = ProductLink(
        supplier_id=supplier_id, product_id=product_id, platform=platform
    )
    session.add(link)
    return link


async def create_cards(ctx: dict, supplier_id: int, job_id: int) -> None:
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

        products = list(
            (
                await session.execute(
                    select(Product).where(
                        Product.supplier_id == supplier_id, Product.id.in_(product_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        settings = await get_or_create_settings(session, supplier_id)
        try:
            price_formula = compile_formula(settings.wb_price_formula)
        except FormulaError as exc:
            job.status = "failed"
            job.message = f"Формула цены WB некорректна: {exc}"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        try:
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
                job.status = "failed"
                job.failed = skipped
                job.message = f"Ни один товар не подошёл под карточку WB (пропущено: {skipped})"
                job.finished_at = datetime.now(UTC)
                await session.commit()
                return

            sent, upload_errors = await client.upload_cards(cards)

            # Карточка принята в очередь — не «активна». Реальный статус даст WB.
            for product in by_vendor.values():
                link = await _link(session, supplier_id, product.id, Platform.WB)
                link.status = IntegrationStatus.PENDING
                link.status_message = "Отправлено на модерацию WB"
                link.barcode = barcode_for(product)
                product.integration_status = IntegrationStatus.PENDING

            job.processed = sent
            await session.commit()

            await asyncio.sleep(SETTLE_SECONDS)

            # Забираем nmID и chrtID. chrtID критичен: остатки FBS WB принимает
            # именно по нему, а не по sku.
            # cards_map возвращает карточки кабинета по vendorCode. Сопоставляем строго
            # по НАШЕМУ vendorCode с префиксом «4D-»: карточки продавца с другим
            # артикулом не совпадут и останутся нетронутыми.
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
                    product.integration_status = IntegrationStatus.ACTIVE
                    ok += 1
                else:
                    # Ещё не проявилась — это нормально, WB обрабатывает с задержкой.
                    link.status = IntegrationStatus.PENDING
                    link.status_message = "Ожидает обработки на стороне WB"
                    product.integration_status = IntegrationStatus.PENDING

            failed = len(by_vendor) - ok - sum(1 for c in by_vendor if c not in errors and c in created)
            job.status = "done"
            job.processed = ok
            job.failed = skipped + len(errors)
            job.finished_at = datetime.now(UTC)

            env = "песочница WB" if client.sandbox else "боевой кабинет WB"
            parts = [f"[{env}] Создано карточек: {ok} из {len(cards)}"]
            if errors:
                parts.append(f"отклонено WB: {len(errors)}")
            if skipped:
                parts.append(f"пропущено: {skipped}")
            if upload_errors:
                parts.append("ошибки отправки: " + "; ".join(upload_errors[:2]))
            job.message = ". ".join(parts)

            session.add(
                LogEntry(
                    supplier_id=supplier_id,
                    job_id=job_id,
                    level="error" if (errors or upload_errors) else "info",
                    platform=Platform.WB,
                    message=job.message,
                    context={"errors": errors},
                )
            )
            await session.commit()

        except (WBError, Exception) as exc:  # noqa: BLE001 — иначе задача зависнет в «running»
            await session.rollback()
            job = await session.get(SyncJob, job_id)
            if job:
                job.status = "failed"
                job.message = str(exc)[:500]
                job.finished_at = datetime.now(UTC)
                session.add(
                    LogEntry(
                        supplier_id=supplier_id,
                        job_id=job_id,
                        level="error",
                        platform=Platform.WB,
                        message=str(exc)[:500],
                    )
                )
                await session.commit()
            raise
