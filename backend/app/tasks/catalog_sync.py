"""Выгрузка каталога 4tochki в products/product_stocks.

Схема выгрузки:
  1. GetFindTyre / GetFindDisk постранично — карточка + цены/остатки по складам (whpr).
  2. GetGoodsInfo пачками по CAE — типоразмеры и индексы, которых нет в поиске,
     но которые нужны для карточки на WB/Ozon.
  3. Пересчёт агрегатов (total_rest, min_price) по складам, выбранным пользователем.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
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
    MissingStrategy,
    Platform,
    Product,
    ProductStock,
    Supplier,
    SyncJob,
    SyncSettings,
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


# Строк на один INSERT. У Postgres потолок 65 535 плейсхолдеров на запрос:
# 11 000 товаров × 10 колонок = 110 000 параметров, и запрос падает.
DB_CHUNK = 2000


async def _upsert_entries(
    session: AsyncSession, supplier_id: int, entries: list[CatalogEntry]
) -> dict[str, int]:
    """Вставляет/обновляет товары и их остатки по складам. Возвращает CAE → product_id."""
    if not entries:
        return {}

    id_by_cae: dict[str, int] = {}

    for start in range(0, len(entries), DB_CHUNK):
        batch = entries[start : start + DB_CHUNK]
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
            for e in batch
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
        id_by_cae.update({cae: pid for pid, cae in result.all()})

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

    for start in range(0, len(stock_rows), DB_CHUNK * 2):
        chunk = stock_rows[start : start + DB_CHUNK * 2]
        s = insert(ProductStock).values(chunk)
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
    """GetGoodsInfo добирает типоразмеры и индексы — без них карточку на МП не собрать.

    Запросы к API идут батчами по 2000 параллельно, запись в БД — одним executemany.
    Раньше здесь был N+1: session.get(Product) на каждый из 22 000 товаров, то есть
    22 000 отдельных SELECT.
    """
    if not id_by_cae:
        return

    items = await client.get_goods_info_all(list(id_by_cae))

    rows = []
    for item in items:
        product_id = id_by_cae.get(item.cae)
        if product_id is None:
            continue
        a = item.attrs
        rows.append(
            {
                "b_id": product_id,
                "attrs": a,
                # Тип товара, бренд, сезон и т.п. заполняет ТОЛЬКО GetGoodsInfo:
                # каталог строится из GetRest (голые коды) + проценки, где этих
                # полей нет. Раньше они приходили из поиска, теперь — отсюда.
                "goods_type": item.goods_type,
                "brand": a.get("brand"),
                "model": a.get("model"),
                "season": a.get("season"),
                "thorn": a.get("thorn"),
                "img_small": a.get("img_small"),
                # У дисков RimContainer.type — целое число (0/1/2), а не тип шины,
                # поэтому заполняем только для шин, иначе фильтр забьётся мусором.
                "tyre_type": a.get("type") if item.goods_type == "tyre" else None,
                "constr": a.get("constr"),
                "camera": a.get("camera"),
                "noise": a.get("noise"),
                "strengthening": a.get("strengthening"),
                "width": _to_num(a.get("width")),
                "height": _to_num(a.get("height")),
                "diameter": _to_num(a.get("diameter")),
                "load_index": a.get("load_index"),
                "speed_index": a.get("speed_index"),
                "weight": _to_num(a.get("weight")),
                "volume": _to_num(a.get("volume")),
                "tn_ved": a.get("tn_ved"),
                "name": a.get("name"),
                "img_big": a.get("img_big"),
            }
        )

    if not rows:
        return

    from sqlalchemy import bindparam, update

    # update(Product.__table__), а не update(Product): при executemany со списком
    # словарей ORM трактует это как bulk-update по первичному ключу и требует id
    # в каждой строке. Нам нужен обычный Core-UPDATE ... WHERE id = :b_id.
    products = Product.__table__

    stmt = (
        update(products)
        .where(products.c.id == bindparam("b_id"))
        .values(
            attrs=bindparam("attrs"),
            goods_type=bindparam("goods_type"),
            brand=bindparam("brand"),
            model=bindparam("model"),
            season=bindparam("season"),
            thorn=bindparam("thorn"),
            img_small=bindparam("img_small"),
            tyre_type=bindparam("tyre_type"),
            constr=bindparam("constr"),
            camera=bindparam("camera"),
            noise=bindparam("noise"),
            strengthening=bindparam("strengthening"),
            width=bindparam("width"),
            height=bindparam("height"),
            diameter=bindparam("diameter"),
            load_index=bindparam("load_index"),
            speed_index=bindparam("speed_index"),
            weight=bindparam("weight"),
            volume=bindparam("volume"),
            tn_ved=bindparam("tn_ved"),
            # Названия и картинки из GetGoodsInfo точнее, чем из поиска, но если
            # там пусто — оставляем то, что уже есть.
            name=func.coalesce(bindparam("name"), products.c.name),
            img_big=func.coalesce(bindparam("img_big"), products.c.img_big),
        )
    )

    for i in range(0, len(rows), 2000):
        # synchronize_session=None обязателен: массовый UPDATE с WHERE по bindparam
        # не умеет синхронизировать объекты сессии, и SQLAlchemy падает без этого.
        await session.execute(
            stmt,
            rows[i : i + 2000],
            execution_options={"synchronize_session": None},
        )


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

    # Транзакционный advisory-lock по поставщику: сериализует параллельные пересчёты
    # (плановый sync_stocks + ручной, смена складов и т.п.). Массовый UPDATE products
    # берёт блокировки строк в недетерминированном порядке, и два процесса ловят
    # deadlock — лок этого не допускает. Освобождается сам в конце транзакции.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:supplier_id)"),
        {"supplier_id": supplier_id},
    )

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


async def _handle_missing(
    session: AsyncSession,
    supplier_id: int,
    seen_caes: set[str],
    strategy: str,
) -> int:
    """Товары, которых не было в этой выгрузке.

    Поиск 4tochki отдаёт только позиции с остатком, поэтому «пропал» обычно значит
    «кончился», а не «снят с продажи» — удалять по умолчанию нельзя, иначе мы
    снесём карточку у товара, который завтра вернётся на склад.
    """
    from sqlalchemy import delete, update

    missing = (
        await session.execute(
            select(Product.id).where(
                Product.supplier_id == supplier_id,
                Product.cae.notin_(seen_caes) if seen_caes else True,
            )
        )
    ).scalars().all()

    if not missing:
        return 0

    if strategy == MissingStrategy.DELETE:
        await session.execute(delete(Product).where(Product.id.in_(missing)))
    else:
        # ZERO_STOCK: карточку и маппинг на маркетплейс сохраняем, обнуляем только остаток.
        await session.execute(
            update(ProductStock)
            .where(ProductStock.product_id.in_(missing))
            .values(rest=0, updated_at=datetime.now(UTC))
        )

    return len(missing)


async def get_or_create_settings(session: AsyncSession, supplier_id: int) -> SyncSettings:
    result = await session.execute(
        select(SyncSettings).where(SyncSettings.supplier_id == supplier_id)
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        settings = SyncSettings(supplier_id=supplier_id)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    return settings


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
        settings = await get_or_create_settings(session, supplier_id)

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

            # CAE, которые уже были в базе до этого прогона — чтобы отличить новые
            # товары от повторно проверенных.
            existing_caes = set(
                (
                    await session.execute(
                        select(Product.cae).where(Product.supplier_id == supplier_id)
                    )
                )
                .scalars()
                .all()
            )

            # Источник ассортимента — GetRest по выбранным складам, а НЕ поиск
            # GetFindTyre: поиск неполон (теряет часть реально имеющихся товаров).
            # По складам, с которых отгружаем, собираем все коды с остатком.
            warehouses = cred.selected_warehouses
            if not warehouses:
                job.status = "done"
                job.message = (
                    "Каталог не собран: не выбрано ни одного склада. "
                    "Отметьте склады в разделе «Подключения»."
                )
                job.finished_at = datetime.now(UTC)
                await _log(session, supplier_id, job_id, "info", job.message)
                await session.commit()
                return

            codes = sorted(await client.get_rest_codes(warehouses))
            job.total = len(codes)
            await session.commit()
            log.info("GetRest по складам %s: кодов с остатком %s", warehouses, len(codes))

            # Цены и остатки по всем складам (для подсказки «где ещё есть сток»
            # проценка отдаёт whpr по всем складам, не только выбранным).
            price_rows = await client.get_price_rest_all(codes)

            # Базовые записи из проценки: cae + остатки/цены по складам. Атрибуты
            # (бренд, тип, размеры) добирает GetGoodsInfo следующим шагом.
            entries = [
                CatalogEntry(cae=pr.cae, goods_type="", warehouses=pr.warehouses)
                for pr in price_rows
                if pr.cae in set(codes)
            ]
            id_by_cae = await _upsert_entries(session, supplier_id, entries)
            job.processed = len(id_by_cae)
            await session.commit()

            await _enrich_attrs(session, client, id_by_cae)
            await session.commit()

            # Обнуляем остаток на выбранных складах у товаров, которых GetRest в этот
            # раз НЕ вернул: раз их нет в остатках склада, значит на нём их нет.
            # GetRest — полный источник по складу (в отличие от старого поиска), поэтому
            # обнуление здесь корректно и не задевает данные невыбранных складов.
            zeroed_stale = await session.execute(
                text(
                    """
                    UPDATE product_stocks s
                    SET rest = 0, updated_at = now()
                    FROM products p
                    WHERE p.id = s.product_id
                      AND p.supplier_id = :supplier_id
                      AND s.wrh = ANY(:warehouses)
                      AND s.rest > 0
                      AND s.updated_at < :started
                    """
                ),
                {
                    "supplier_id": supplier_id,
                    "warehouses": warehouses,
                    "started": job.started_at,
                },
            )
            log.info("Каталог: обнулено устаревших строк остатка: %s", zeroed_stale.rowcount)

            # Снимок агрегатов ДО пересчёта — чтобы показать движение остатка и цен
            # после этого прогона (та же сводка, что у задачи «Цены и остатки»).
            before = {
                pid: (tr, mp)
                for pid, tr, mp in (
                    await session.execute(
                        select(Product.id, Product.total_rest, Product.min_price).where(
                            Product.supplier_id == supplier_id
                        )
                    )
                ).all()
            }
            before_sum = sum(v[0] for v in before.values())

            await recompute_aggregates(session, supplier_id, cred.selected_warehouses)

            after = (
                await session.execute(
                    select(Product.id, Product.total_rest, Product.min_price).where(
                        Product.supplier_id == supplier_id
                    )
                )
            ).all()
            after_sum = sum(tr for _, tr, _ in after)
            zeroed = sum(
                1 for pid, tr, _ in after if pid in before and before[pid][0] > 0 and tr == 0
            )
            price_changes = sum(
                1 for pid, _, mp in after if pid in before and before[pid][1] != mp
            )

            new_count = len(set(id_by_cae) - existing_caes)
            supplier.catalog_synced_at = datetime.now(UTC)
            job.status = "done"
            job.finished_at = datetime.now(UTC)
            parts = [
                f"Каталог проверен: {len(id_by_cae)} позиций",
                f"Новых {new_count}",
                f"Обнулено остатков: {zeroed}",
                f"Остаток до {before_sum}, после {after_sum}",
                f"Обновлено цен: {price_changes}",
            ]
            job.message = ". ".join(parts) + "."
            if not cred.selected_warehouses:
                job.message += " Склады не выбраны — остатки обнулены."
            await _log(session, supplier_id, job_id, "info", job.message)
            await session.commit()

        except Exception as exc:  # noqa: BLE001 — иначе задача навсегда зависнет в «running»
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
