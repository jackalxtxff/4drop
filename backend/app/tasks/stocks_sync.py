"""Обновление цен и остатков по уже известным товарам.

Отдельно от выгрузки каталога и намеренно дешевле её: каталог тянет карточки и
атрибуты, а здесь мы спрашиваем только цену и остаток по CAE, которые уже лежат
в базе. Именно эта задача защищает от оверселла, поэтому она должна ходить часто.

Замеры на каталоге в 22 000 позиций (см. docs/4tochki-api.md):
  * батч 200 последовательно — ~90 с;
  * батч 2000 при 6 параллельных запросах — ~5 с.
Оба предела найдены экспериментом: >2000 элементов API отвергает ошибкой [51],
выше ~6 потоков их сервер не ускоряется.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.fourtochki.client import FourTochkiClient, FourTochkiError
from app.models import (
    Credential,
    LogEntry,
    MissingStrategy,
    Platform,
    Product,
    ProductStock,
    SyncJob,
)
from app.tasks.catalog_sync import get_or_create_settings
from app.security import decrypt_secret
from app.tasks.catalog_sync import recompute_aggregates

log = logging.getLogger(__name__)

# Строк на один INSERT. Postgres плохо переваривает многотысячные VALUES-списки,
# а 22 000 строк одним запросом упираются в лимит параметров.
DB_CHUNK = 5000


async def _upsert_stocks(session: AsyncSession, rows: list[dict]) -> None:
    # Один порядок блокировки строк во всех прогонах: два параллельных upsert,
    # трогающих одни и те же (product_id, wrh) в разной последовательности, иначе
    # могут поймать deadlock. Сортировка делает порядок детерминированным.
    rows = sorted(rows, key=lambda r: (r["product_id"], r["wrh"]))
    for i in range(0, len(rows), DB_CHUNK):
        chunk = rows[i : i + DB_CHUNK]
        stmt = insert(ProductStock).values(chunk)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[ProductStock.product_id, ProductStock.wrh],
                set_={
                    "rest": stmt.excluded.rest,
                    "price": stmt.excluded.price,
                    "price_rozn": stmt.excluded.price_rozn,
                    "updated_at": datetime.now(UTC),
                },
            )
        )


async def _zero_stale(session: AsyncSession, supplier_id: int, started: datetime) -> int:
    """Обнулить строки, которых поставщик в этот раз не подтвердил.

    Признак — updated_at раньше начала задачи: всё, что пришло из API, мы только что
    перезаписали. Считается одним UPDATE в БД, а не выгрузкой 32 000 строк в Python.

    Строки не удаляем: товар вернётся на склад, и строка обновится на месте.
    """
    result = await session.execute(
        text(
            """
            UPDATE product_stocks s
            SET rest = 0, updated_at = now()
            FROM products p
            WHERE p.id = s.product_id
              AND p.supplier_id = :supplier_id
              AND s.rest > 0
              AND s.updated_at < :started
            """
        ),
        {"supplier_id": supplier_id, "started": started},
    )
    return result.rowcount or 0


async def sync_stocks(ctx: dict, supplier_id: int, job_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        started = datetime.now(UTC)
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
            await session.commit()
            return

        secrets = json.loads(decrypt_secret(cred.secrets_encrypted))
        settings = await get_or_create_settings(session, supplier_id)

        id_by_cae = dict(
            (
                await session.execute(
                    select(Product.cae, Product.id).where(Product.supplier_id == supplier_id)
                )
            ).all()
        )
        if not id_by_cae:
            job.status = "done"
            job.message = "Каталог пуст — обновлять нечего"
            job.finished_at = datetime.now(UTC)
            await session.commit()
            return

        job.total = len(id_by_cae)
        await session.commit()

        try:
            client = FourTochkiClient(secrets["login"], secrets["password"])

            # Спрашиваем ВСЕ склады, а не только выбранные: выбор применяется позже,
            # при расчёте агрегатов. Иначе данные по невыбранным складам затрутся,
            # и смена набора складов перестанет быть мгновенной.
            rows = await client.get_price_rest_all(list(id_by_cae))

            stock_rows = [
                {
                    "product_id": id_by_cae[r.cae],
                    "wrh": w.wrh,
                    "rest": w.rest,
                    "price": w.price,
                    "price_rozn": w.price_rozn,
                }
                for r in rows
                if r.cae in id_by_cae
                for w in r.warehouses
            ]

            await _upsert_stocks(session, stock_rows)
            job.processed = len(rows)
            await session.commit()

            zeroed = await _zero_stale(session, supplier_id, started)

            # Товары, которых проценка не вернула ВООБЩЕ — 4tochki их больше не знает
            # (сняты с ассортимента). Это авторитетный признак «пропал», в отличие от
            # отсутствия в выдаче поиска. При стратегии delete — удаляем; при
            # zero_stock их остатки уже обнулены выше (_zero_stale).
            gone = [cae for cae in id_by_cae if cae not in {r.cae for r in rows}]
            removed = 0
            if gone and settings and settings.missing_strategy == MissingStrategy.DELETE:
                res = await session.execute(
                    delete(Product).where(
                        Product.supplier_id == supplier_id, Product.cae.in_(gone)
                    )
                )
                removed = res.rowcount or 0

            await recompute_aggregates(session, supplier_id, cred.selected_warehouses)

            elapsed = (datetime.now(UTC) - started).total_seconds()
            job.status = "done"
            job.finished_at = datetime.now(UTC)
            job.message = (
                f"Обновлено позиций: {len(rows)} за {elapsed:.0f} с. "
                f"Обнулено остатков: {zeroed}."
            )
            if gone:
                job.message += (
                    f" Снято 4tochki: {len(gone)}"
                    + (f" (удалено {removed})" if removed else " (остаток обнулён)")
                )
            session.add(
                LogEntry(
                    supplier_id=supplier_id,
                    job_id=job_id,
                    level="info",
                    platform=Platform.FOURTOCHKI,
                    message=job.message,
                )
            )
            await session.commit()

        except Exception as exc:  # noqa: BLE001 — иначе задача навсегда зависнет в «running»
            await session.rollback()
            job = await session.get(SyncJob, job_id)
            if job:
                job.status = "failed"
                job.message = str(exc)
                job.finished_at = datetime.now(UTC)
                session.add(
                    LogEntry(
                        supplier_id=supplier_id,
                        job_id=job_id,
                        level="error",
                        platform=Platform.FOURTOCHKI,
                        message=str(exc),
                    )
                )
                await session.commit()
            raise
