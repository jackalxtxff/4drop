"""Выгрузка каталога 4tochki в products/product_stocks.

Схема выгрузки:
  1. GetFindTyre / GetFindDisk постранично — карточка + цены/остатки по складам (whpr).
  2. GetGoodsInfo пачками по CAE — типоразмеры и индексы, которых нет в поиске,
     но которые нужны для карточки на WB/Ozon.
  3. Пересчёт агрегатов (total_rest, min_price) по складам, выбранным пользователем.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.integrations.fourtochki.client import (
    CatalogEntry,
    FourTochkiClient,
    FourTochkiError,
)
from app.models import (
    Credential,
    LogEntry,
    Platform,
    Product,
    ProductStock,
    Supplier,
    SyncJob,
)
from app.security import decrypt_secret

log = logging.getLogger(__name__)

MAX_PAGES = 500  # предохранитель от бесконечного цикла, если totalPages придёт мусором


async def _log(
    session: AsyncSession,
    supplier_id: int,
    job_id: int | None,
    level: str,
    message: str,
    **context: Any,
) -> None:
    session.add(
        LogEntry(
            supplier_id=supplier_id,
            job_id=job_id,
            level=level,
            platform=Platform.FOURTOCHKI,
            message=message,
            context=context,
        )
    )


def _to_num(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


async def _upsert_entries(
    session: AsyncSession, supplier_id: int, entries: list[CatalogEntry]
) -> dict[str, int]:
    """Вставляет/обновляет товары и их остатки по складам. Возвращает CAE → product_id."""
    if not entries:
        return {}

    rows = [
        {
            "supplier_id": supplier_id,
            "cae": e.cae,
            "goods_type": e.goods_type,
            "brand": e.brand,
            "model": e.model,
            "name": e.name,
            "season": e.season,
            "thorn": e.thorn,
            "img_small": e.img_small,
            "img_big": e.img_big,
        }
        for e in entries
    ]

    stmt = insert(Product).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Product.supplier_id, Product.cae],
        set_={
            "goods_type": stmt.excluded.goods_type,
            "brand": stmt.excluded.brand,
            "model": stmt.excluded.model,
            "name": stmt.excluded.name,
            "season": stmt.excluded.season,
            "thorn": stmt.excluded.thorn,
            "img_small": stmt.excluded.img_small,
            "img_big": stmt.excluded.img_big,
            "updated_at": datetime.now(UTC),
        },
    ).returning(Product.id, Product.cae)

    result = await session.execute(stmt)
    id_by_cae = {cae: pid for pid, cae in result.all()}

    stock_rows = [
        {
            "product_id": id_by_cae[e.cae],
            "wrh": w.wrh,
            "rest": w.rest,
            "price": w.price,
            "price_rozn": w.price_rozn,
        }
        for e in entries
        if e.cae in id_by_cae
        for w in e.warehouses
    ]

    if stock_rows:
        s = insert(ProductStock).values(stock_rows)
        await session.execute(
            s.on_conflict_do_update(
                index_elements=[ProductStock.product_id, ProductStock.wrh],
                set_={
                    "rest": s.excluded.rest,
                    "price": s.excluded.price,
                    "price_rozn": s.excluded.price_rozn,
                    "updated_at": datetime.now(UTC),
                },
            )
        )

    return id_by_cae


async def _enrich_attrs(
    session: AsyncSession,
    client: FourTochkiClient,
    id_by_cae: dict[str, int],
) -> None:
    """GetGoodsInfo добирает типоразмеры и индексы — без них карточку на МП не собрать."""
    batch = get_settings().fourtochki_batch_size
    codes = list(id_by_cae)

    for start in range(0, len(codes), batch):
        chunk = codes[start : start + batch]
        items = await client.get_goods_info(chunk)

        for item in items:
            product_id = id_by_cae.get(item.cae)
            if product_id is None:
                continue
            a = item.attrs
            product = await session.get(Product, product_id)
            if product is None:
                continue

            product.attrs = a
            product.width = _to_num(a.get("width"))
            product.height = _to_num(a.get("height"))
            product.diameter = _to_num(a.get("diameter"))
            product.load_index = a.get("load_index")
            product.speed_index = a.get("speed_index")
            product.weight = _to_num(a.get("weight"))
            product.volume = _to_num(a.get("volume"))
            product.tn_ved = a.get("tn_ved")
            # Названия и картинки из GetGoodsInfo точнее, чем из поиска.
            product.name = a.get("name") or product.name
            product.img_big = a.get("img_big") or product.img_big


async def recompute_aggregates(
    session: AsyncSession, supplier_id: int, selected_warehouses: list[int]
) -> None:
    """Пересчёт total_rest и min_price по выбранным складам.

    Вынесено отдельно: при смене набора складов агрегаты надо пересчитать,
    но перевыкачивать каталог не нужно — сырьё лежит в product_stocks.
    """
    if selected_warehouses:
        wrh_filter = "AND s.wrh = ANY(:warehouses)"
        params = {"supplier_id": supplier_id, "warehouses": selected_warehouses}
    else:
        # Складов не выбрано — продавать нечего. Обнуляем остатки, а не берём все склады:
        # молчаливое «все склады» опубликовало бы позиции с недостижимым сроком поставки.
        wrh_filter = "AND FALSE"
        params = {"supplier_id": supplier_id}

    from sqlalchemy import text

    await session.execute(
        text(
            f"""
            UPDATE products p
            SET total_rest = COALESCE(agg.total_rest, 0),
                min_price  = agg.min_price,
                price_rozn = agg.price_rozn
            FROM (
                SELECT p2.id AS product_id,
                       SUM(s.rest)                          AS total_rest,
                       MIN(s.price) FILTER (WHERE s.rest > 0) AS min_price,
                       MIN(s.price_rozn) FILTER (WHERE s.rest > 0) AS price_rozn
                FROM products p2
                LEFT JOIN product_stocks s
                       ON s.product_id = p2.id {wrh_filter}
                WHERE p2.supplier_id = :supplier_id
                GROUP BY p2.id
            ) AS agg
            WHERE p.id = agg.product_id
            """
        ),
        params,
    )


async def sync_catalog(ctx: dict, supplier_id: int, job_id: int) -> None:
    """arq-задача: полная выгрузка каталога поставщика."""
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        supplier = await session.get(Supplier, supplier_id)
        if job is None or supplier is None:
            return

        job.status = "running"
        await session.commit()

        cred = (
            await session.execute(
                select(Credential).where(
                    Credential.supplier_id == supplier_id,
                    Credential.platform == Platform.FOURTOCHKI,
                )
            )
        ).scalar_one_or_none()

        if cred is None or not cred.secrets_encrypted:
            job.status = "failed"
            job.message = "Доступы к 4tochki не заданы"
            job.finished_at = datetime.now(UTC)
            await _log(session, supplier_id, job_id, "error", job.message)
            await session.commit()
            return

        secrets = json.loads(decrypt_secret(cred.secrets_encrypted))

        try:
            client = FourTochkiClient(secrets["login"], secrets["password"])

            # Сначала Ping. На неверных доступах методы каталога у 4tochki падают
            # серверным NullReferenceException, а не внятной ошибкой авторизации —
            # без этой проверки пользователь увидит .NET-стектрейс вместо «проверьте логин».
            if not await client.ping():
                job.status = "failed"
                job.message = (
                    "4tochki отклонили логин или пароль. Проверьте доступы "
                    "в разделе «Подключения»."
                )
                job.finished_at = datetime.now(UTC)
                await _log(session, supplier_id, job_id, "error", job.message)
                await session.commit()
                return

            processed = 0

            for finder, label in (
                (client.find_tyres, "шины"),
                (client.find_rims, "диски"),
            ):
                page = 1
                total_pages = 1

                while page <= total_pages and page <= MAX_PAGES:
                    entries, reported = await finder(page=page)
                    if page == 1 and reported:
                        total_pages = min(reported, MAX_PAGES)
                        job.total += reported

                    if not entries:
                        break

                    id_by_cae = await _upsert_entries(session, supplier_id, entries)
                    await _enrich_attrs(session, client, id_by_cae)

                    processed += 1
                    job.processed = processed
                    await session.commit()

                    log.info(
                        "4tochki %s: страница %s/%s, позиций %s",
                        label, page, total_pages, len(entries),
                    )
                    page += 1

            await recompute_aggregates(session, supplier_id, cred.selected_warehouses)

            supplier.catalog_synced_at = datetime.now(UTC)
            job.status = "done"
            job.finished_at = datetime.now(UTC)
            job.message = f"Каталог обновлён, страниц обработано: {processed}"
            if not cred.selected_warehouses:
                job.message += ". Склады не выбраны — остатки обнулены."
            await _log(session, supplier_id, job_id, "info", job.message)
            await session.commit()

        except FourTochkiError as exc:
            await session.rollback()
            job = await session.get(SyncJob, job_id)
            if job:
                job.status = "failed"
                job.failed += 1
                job.message = str(exc)
                job.finished_at = datetime.now(UTC)
                await _log(session, supplier_id, job_id, "error", str(exc))
                await session.commit()
            raise
