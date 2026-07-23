"""Обновление атрибутов уже созданных карточек WB.

Отдельно от создания (create_cards) и от пуша цен/остатков (push_marketplaces):
здесь синхронизируются характеристики, название и картинки, когда они изменились
в 4tochki после того, как карточка уже создана.

Ключевая экономия — хэш атрибутивной части (ProductLink.card_hash): карточку
досылаем на площадку ТОЛЬКО если хэш изменился. Иначе каждый прогон гонял бы все
карточки на повторную модерацию впустую. Цена в хэш не входит — её обновляет пуш.

Обновление на стороне WB — тот же upload_cards по vendorCode (Content API делает
upsert). После отправки карточка снова уходит на модерацию, поэтому запускать эту
задачу часто нет смысла — раз в сутки достаточно.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.formula import FormulaError, compile_formula, evaluate
from app.integrations.wb.cards import (
    SUBJECT_BY_TYPE,
    CardBuildError,
    build_card,
    card_content_hash,
    resolve_wb_brand,
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
from app.security import decrypt_secret
from app.tasks.catalog_sync import get_or_create_settings


async def _update_wb_cards(
    session: AsyncSession, supplier_id: int, job: SyncJob
) -> tuple[str, str]:
    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.WB,
            )
        )
    ).scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        return "error", "Доступы к Wildberries не заданы"

    api_key = json.loads(decrypt_secret(cred.secrets_encrypted))["api_key"]
    brand_map = (cred.settings or {}).get("wb_brand_map") or {}
    settings = await get_or_create_settings(session, supplier_id)
    try:
        price_formula = compile_formula(settings.wb_price_formula)
    except FormulaError as exc:
        return "error", f"Формула цены WB некорректна: {exc}"

    # Только активные карточки: у pending/error ещё нет утверждённой карточки на площадке.
    rows = (
        await session.execute(
            select(ProductLink, Product)
            .join(Product, Product.id == ProductLink.product_id)
            .where(
                ProductLink.supplier_id == supplier_id,
                ProductLink.platform == Platform.WB,
                ProductLink.status == IntegrationStatus.ACTIVE,
                # Заблокированные не обновляем — их не трогаем на маркетплейсе вовсе.
                Product.sync_blocked.is_(False),
            )
        )
    ).all()

    if not rows:
        return "info", "Нет активных карточек WB — обновлять нечего"

    job.total = len(rows)

    client = WBClient(api_key)

    # Реестры брендов категорий — чтобы бренд ушёл в точном написании WB.
    subjects = {SUBJECT_BY_TYPE.get(p.goods_type) for _l, p in rows}
    subjects.discard(None)
    brand_registry: dict[int, dict[str, str]] = {}
    try:
        for sid in subjects:
            brand_registry[sid] = await client.list_brands(sid)
    except WBError as exc:
        return "error", f"Не удалось получить реестр брендов WB: {exc}"

    changed_cards: list[dict] = []
    changed_links: list[tuple[ProductLink, Product, str]] = []
    build_errors = 0

    for link, product in rows:
        new_hash = card_content_hash(product)
        if new_hash == link.card_hash:
            continue  # атрибуты не менялись — не трогаем карточку

        price = (
            evaluate(price_formula, product.min_price, product.price_rozn, product.weight)
            if product.min_price
            else None
        )
        subject_id = SUBJECT_BY_TYPE.get(product.goods_type)
        wb_brand = resolve_wb_brand(product.brand, brand_registry.get(subject_id), brand_map)
        try:
            # Обновление идёт по существующему штрихкоду связи (WB не выдаёт новый).
            card = build_card(product, price, link.barcode or "", wb_brand)
        except CardBuildError:
            build_errors += 1
            continue
        # Обновление идёт по nmID: WB правит существующую карточку, а не создаёт новую.
        variant = card["variants"][0]
        variant["nmID"] = link.nm_id
        # Указываем chrtID существующего размера: без него WB считает штрихкод из sizes
        # НОВЫМ и отклоняет карточку как «неуникальный баркод» (он уже на этой карточке).
        # Заодно это чинило проваленную модерацию, из-за которой не применялся kizMarked.
        if link.chrt_id:
            for size in variant.get("sizes", []):
                size["chrtID"] = link.chrt_id
        changed_cards.append(variant)
        changed_links.append((link, product, new_hash))

    if not changed_cards:
        return "info", f"Атрибуты не изменились — обновлять нечего (карточек: {len(rows)})"

    sent, errors = await client.update_cards(changed_cards)

    # upload_cards принимает всю пачку целиком (или падает), поэтому при успехе
    # обновляем хэш у всех отправленных: повторно слать их до следующей правки не нужно.
    if sent:
        now = datetime.now(UTC)
        for link, product, new_hash in changed_links:
            link.card_hash = new_hash
            link.status = IntegrationStatus.PENDING
            link.status_message = "Обновление отправлено на модерацию WB"
            product.integration_status = IntegrationStatus.PENDING
            link.updated_at = now
        job.processed = len(changed_links)

    env = "песочница WB" if client.sandbox else "боевой кабинет WB"
    parts = [f"[{env}] Обновлено карточек: {len(changed_links)} из {len(rows)}"]
    if build_errors:
        parts.append(f"не собралось: {build_errors}")
    if errors:
        parts.append("ошибки: " + "; ".join(errors[:2]))

    level = "error" if (errors and not sent) else "info"
    return level, ". ".join(parts)


async def update_cards(ctx: dict, supplier_id: int, job_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        job.status = "running"
        await session.commit()

        try:
            level, message = await _update_wb_cards(session, supplier_id, job)
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
