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

    async def _attach_photo(link: ProductLink, product: Product) -> None:
        img = await _fetch_image(product.img_big or product.img_small)
        if img and link.nm_id:
            await client.upload_photo(link.nm_id, img)

    # Что УЖЕ есть в кабинете под нашими vendorCode. Критично: WB отклоняет ВЕСЬ батч
    # upload, если хоть один vendorCode уже занят («vendor code is used in other cards»).
    # Поэтому существующие карточки не пересоздаём, а привязываемся к ним.
    existing_cards = {vc: c for vc, c in (await client.cards_map()).items() if is_ours(vc)}

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

    # Реестры брендов по категориям — чтобы бренд ушёл в точном написании WB.
    subjects = {SUBJECT_BY_TYPE.get(p.goods_type) for p in products}
    subjects.discard(None)
    brand_registry: dict[int, dict[str, str]] = {}
    try:
        for sid in subjects:
            brand_registry[sid] = await client.list_brands(sid)
    except WBError as exc:
        return "error", f"Не удалось получить реестр брендов WB: {exc}"

    # Штрихкоды генерирует WB. Нужны только НОВЫМ товарам (у существующих берём из
    # карточки кабинета), у кого ещё нет годного EAN в связи.
    new_needing_barcode = sum(
        1
        for p in products
        if vendor_code(p) not in existing_cards
        and not _reusable_barcode(existing_links.get(p.id))
    )
    try:
        fresh = iter(await client.generate_barcodes(new_needing_barcode))
    except WBError as exc:
        return "error", f"Не удалось получить штрихкоды WB: {exc}"

    cards: list[dict] = []
    by_vendor: dict[str, Product] = {}
    barcode_by_product: dict[int, str] = {}
    skipped = 0
    linked = 0  # карточки, которые уже были в кабинете и к которым привязались

    for product in products:
        vc = vendor_code(product)
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

        # Карточка уже существует в кабинете — привязываем, не пересоздаём (иначе WB
        # отклонит весь батч).
        exist = existing_cards.get(vc)
        if exist:
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.nm_id = exist["nm_id"]
            link.chrt_id = exist["chrt_id"]
            link.barcode = exist["barcode"] or link.barcode
            link.status = IntegrationStatus.ACTIVE
            link.status_message = None
            link.card_hash = card_content_hash(product)
            product.integration_status = IntegrationStatus.ACTIVE
            await _attach_photo(link, product)
            linked += 1
            continue

        # Новая карточка.
        barcode = _reusable_barcode(existing_links.get(product.id)) or next(fresh)
        price = (
            evaluate(price_formula, product.min_price, product.price_rozn, product.weight)
            if product.min_price
            else None
        )
        try:
            cards.append(build_card(product, price, barcode, wb_brand))
        except CardBuildError as exc:
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.status = IntegrationStatus.ERROR
            link.status_message = str(exc)
            product.integration_status = IntegrationStatus.ERROR
            skipped += 1
            continue
        by_vendor[vc] = product
        barcode_by_product[product.id] = barcode

    await session.commit()

    ok = linked
    errors: dict[str, str] = {}
    upload_errors: list[str] = []

    if cards:
        _sent, upload_errors = await client.upload_cards(cards)
        for product in by_vendor.values():
            link = await _link(session, supplier_id, product.id, Platform.WB)
            link.status = IntegrationStatus.PENDING
            link.status_message = "Отправлено на модерацию WB"
            link.barcode = barcode_by_product[product.id]
            product.integration_status = IntegrationStatus.PENDING
        await session.commit()

        await asyncio.sleep(SETTLE_SECONDS)

        # Забираем nmID только что созданных карточек и прикрепляем им фото.
        created = {vc: c for vc, c in (await client.cards_map()).items() if is_ours(vc)}
        errors = await client.card_errors()
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
                await _attach_photo(link, product)
                ok += 1
            else:
                # nmID ещё не присвоен — дорезолвит reconcile_wb_pending (в пуше).
                link.status = IntegrationStatus.PENDING
                link.status_message = "Ожидает обработки на стороне WB"
                product.integration_status = IntegrationStatus.PENDING

    await session.commit()

    if ok == 0 and not by_vendor:
        return "error", f"Ни один товар не подошёл под карточку WB (пропущено: {skipped})"

    job.processed = ok
    job.failed = skipped + len(errors)

    env = "песочница WB" if client.sandbox else "боевой кабинет WB"
    parts = [f"[{env}] Готово карточек: {ok} из {len(products)}"]
    if linked:
        parts.append(f"уже были, привязано: {linked}")
    if errors:
        parts.append(f"отклонено WB: {len(errors)}")
    if skipped:
        parts.append(f"пропущено: {skipped}")
    if upload_errors:
        parts.append("ошибки отправки: " + "; ".join(upload_errors[:2]))

    level = "error" if (errors or upload_errors) and ok == 0 else "info"
    return level, ". ".join(parts)


async def reconcile_wb_pending(session: AsyncSession, supplier_id: int, api_key: str) -> int:
    """Дорезолвить WB-связи в статусе pending и прикрепить им фото.

    Карточка появляется в кабинете WB с задержкой, поэтому на 10-сек окне создания
    nmID мог быть ещё не присвоен — связь осталась pending. Здесь находим карточку по
    vendorCode, проставляем nmID/chrtID/штрихкод, переводим в active и грузим фото
    (при создании фото не прикрепить без nmID). Без этого шага pending-карточки навсегда
    остаются без nmID: их не пушат (пуш берёт только active) и они без фото.

    Возвращает число связей, повышенных до active.
    """
    rows = (
        await session.execute(
            select(ProductLink, Product)
            .join(Product, Product.id == ProductLink.product_id)
            .where(
                ProductLink.supplier_id == supplier_id,
                ProductLink.platform == Platform.WB,
                ProductLink.status == IntegrationStatus.PENDING,
            )
        )
    ).all()
    if not rows:
        return 0

    client = WBClient(api_key)
    created = {vc: c for vc, c in (await client.cards_map()).items() if is_ours(vc)}
    errors = await client.card_errors()

    promoted = 0
    for link, product in rows:
        vc = vendor_code(product)
        if vc in errors:
            link.status = IntegrationStatus.ERROR
            link.status_message = errors[vc]
            product.integration_status = IntegrationStatus.ERROR
            continue
        card = created.get(vc)
        if not card:
            continue  # ещё не проиндексирована — дождёмся следующего прогона

        link.nm_id = card["nm_id"]
        link.chrt_id = card["chrt_id"]
        link.barcode = card["barcode"] or link.barcode
        link.status = IntegrationStatus.ACTIVE
        link.status_message = None
        link.card_hash = card_content_hash(product)
        product.integration_status = IntegrationStatus.ACTIVE

        img = await _fetch_image(product.img_big or product.img_small)
        if img:
            await client.upload_photo(link.nm_id, img)
        promoted += 1

    await session.commit()
    return promoted


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
