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

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.formula import FormulaError, compile_formula, evaluate
from app.integrations.wb.cards import (
    SUBJECT_BY_TYPE,
    CardBuildError,
    build_card,
    card_content_hash,
    is_ours,
    resolve_wb_brand,
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


# 4tochki отдаёт картинки только браузерному User-Agent (иначе 502), поэтому качаем
# с этим заголовком, а в WB грузим уже байтами.
_BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


async def _fetch_image(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": _BROWSER_UA}
        ) as http:
            resp = await http.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
        return resp.content
    return None


def _reusable_barcode(link: ProductLink | None) -> str | None:
    """Штрихкод связи, годный к переиспользованию — только настоящий EAN (цифры).

    Старые карточки могли получить самодельный штрихкод вида «4D<cae>» (до перехода на
    штрихкоды WB). Такой переиспользовать нельзя — по нему WB не примет остатки; для
    таких генерируем новый EAN средствами WB.
    """
    bc = link.barcode if link else None
    return bc if (bc and bc.isdigit()) else None


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
    brand_map: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Ядро создания карточек WB. Возвращает (level, message).

    Заблокированные товары сюда попадать не должны — их отсекают вызывающие.
    brand_map — соответствие бренд каталога → бренд в реестре WB (см. resolve_wb_brand).
    """
    try:
        price_formula = compile_formula(settings.wb_price_formula)
    except FormulaError as exc:
        return "error", f"Формула цены WB некорректна: {exc}"

    client = WBClient(api_key)

    # Штрихкоды генерирует WB (вручную нельзя — иначе остатки по ним не пройдут).
    # Повторный прогон переиспользует уже выданный штрихкод из связи, чтобы WB не
    # создал дубль карточки; новые генерируем одним запросом на всю пачку.
    existing_links = {
        l.product_id: l
        for l in (
            await session.execute(
                select(ProductLink).where(
                    ProductLink.product_id.in_([p.id for p in products]),
                    ProductLink.platform == Platform.WB,
                )
            )
        ).scalars()
    }
    need_count = sum(
        1 for p in products if not _reusable_barcode(existing_links.get(p.id))
    )
    try:
        fresh = iter(await client.generate_barcodes(need_count))
    except WBError as exc:
        return "error", f"Не удалось получить штрихкоды WB: {exc}"

    # Реестры брендов по категориям (шины/диски) — один запрос на категорию. Нужны,
    # чтобы подставить бренд в точном написании WB (иначе «бренда нет на WB»).
    subjects = {SUBJECT_BY_TYPE.get(p.goods_type) for p in products}
    subjects.discard(None)
    brand_registry: dict[int, dict[str, str]] = {}
    try:
        for sid in subjects:
            brand_registry[sid] = await client.list_brands(sid)
    except WBError as exc:
        return "error", f"Не удалось получить реестр брендов WB: {exc}"

    cards: list[dict] = []
    by_vendor: dict[str, Product] = {}
    barcode_by_product: dict[int, str] = {}
    skipped = 0

    for product in products:
        barcode = _reusable_barcode(existing_links.get(product.id)) or next(fresh)
        price = (
            evaluate(price_formula, product.min_price, product.price_rozn, product.weight)
            if product.min_price
            else None
        )

        subject_id = SUBJECT_BY_TYPE.get(product.goods_type)
        wb_brand = resolve_wb_brand(product.brand, brand_registry.get(subject_id), brand_map)
        if subject_id and product.brand and not wb_brand:
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.status = IntegrationStatus.ERROR
            link.status_message = (
                f"Бренд «{product.brand}» не найден в реестре WB для этой категории — "
                "добавьте бренд в кабинете WB или задайте соответствие."
            )
            product.integration_status = IntegrationStatus.ERROR
            skipped += 1
            continue

        try:
            cards.append(build_card(product, price, barcode, wb_brand))
        except CardBuildError as exc:
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.status = IntegrationStatus.ERROR
            link.status_message = str(exc)
            product.integration_status = IntegrationStatus.ERROR
            skipped += 1
            continue
        by_vendor[vendor_code(product)] = product
        barcode_by_product[product.id] = barcode

    await session.commit()

    if not cards:
        return "error", f"Ни один товар не подошёл под карточку WB (пропущено: {skipped})"

    sent, upload_errors = await client.upload_cards(cards)

    for product in by_vendor.values():
        link = await _link(session, supplier_id, product.id, Platform.WB)
        link.status = IntegrationStatus.PENDING
        link.status_message = "Отправлено на модерацию WB"
        link.barcode = barcode_by_product[product.id]
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
            # Фото: скачиваем у 4tochki (браузерный UA) и грузим байтами в WB. Прикрепить
            # можно только когда у карточки уже есть nmID. Ошибку фото не превращаем в
            # ошибку карточки — карточка уже создана.
            img = await _fetch_image(product.img_big or product.img_small)
            if img:
                await client.upload_photo(link.nm_id, img)
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
                session, supplier_id, products, api_key, settings, job,
                brand_map=(cred.settings or {}).get("wb_brand_map") or {},
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
