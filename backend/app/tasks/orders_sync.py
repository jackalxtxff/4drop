"""Синхронизация заказов с маркетплейсов и оформление их у поставщика.

Ядро вынесено сюда, чтобы одним кодом пользовались и кнопка «Обновить заказы», и
плановая задача. Вебхуков по заказам у WB нет (в их списке событий только карточки,
отзывы и отчёты), поэтому единственный способ узнать о заказе — опрос. У FBS жёсткий
дедлайн сборки, так что опрашивать нужно часто.

Оформление заказа у поставщика идёт через ТЕСТОВЫЙ контур 4tochki
(CreateOrder is_test=True) — реальной отгрузки не происходит.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.connections import _mp_client, load_secrets
from app.db import SessionLocal
from app.integrations.fourtochki.client import FourTochkiClient, FourTochkiError, OrderLine
from app.integrations.ozon.client import OzonError
from app.integrations.wb.client import WBError
from app.models import (
    Credential,
    LogEntry,
    Order,
    Platform,
    Product,
    ProductLink,
    ProductStock,
    SyncJob,
    WarehouseMapping,
)
from app.schemas import OrdersSyncPlatform
from app.tasks.catalog_sync import get_or_create_settings

log = logging.getLogger(__name__)

# Площадки, с которых умеем тянуть заказы. Ozon — основа (без песочницы).
MP_PLATFORMS = (Platform.WB, Platform.OZON)


async def fourtochki_wh_names(session: AsyncSession, supplier_id: int) -> dict[int, str]:
    """id склада 4tochki → имя, из справочника в кредах (для подписи источника)."""
    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.FOURTOCHKI,
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        return {}
    return {w["id"]: w.get("name") or w.get("short_name") or str(w["id"]) for w in cred.warehouses}


async def fbs_wh_names(session: AsyncSession, supplier_id: int, platform: str) -> dict[str, str]:
    """id FBS-склада → его наименование, из кэша складов в кредах площадки.

    В заказе площадка отдаёт только id склада, а показывать пользователю нужно имя
    («4drop Москва», а не 35498) — иначе по заказу не понять, из какого он города.
    """
    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id, Credential.platform == platform
            )
        )
    ).scalar_one_or_none()
    if cred is None:
        return {}
    return {
        str(w["id"]): w.get("name")
        for w in ((cred.settings or {}).get("fbs_warehouses") or [])
        if w.get("name")
    }


async def order_client(session: AsyncSession, supplier_id: int, platform: str):
    """Клиент площадки для выгрузки заказов, или None если доступов нет."""
    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id, Credential.platform == platform
            )
        )
    ).scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        return None
    return _mp_client(platform, load_secrets(cred))


def pick_source(
    fbs_warehouse_id: str | None,
    product_id: int | None,
    mapping: dict[str, list[tuple[int, int]]],
    stock: dict[tuple[int, int], int],
) -> int | None:
    """Выбрать склад 4tochki для заказа: среди привязанных к FBS-складу берём первый
    по приоритету, у которого есть остаток на этот товар; если ни у кого нет — первый
    по приоритету (чтобы источник всё равно был виден). None, если привязок нет.
    """
    candidates = mapping.get(fbs_warehouse_id or "", [])
    if not candidates:
        return None
    if product_id is not None:
        for _prio, wrh in candidates:
            if stock.get((product_id, wrh), 0) > 0:
                return wrh
    return candidates[0][1]


async def pull_orders(session: AsyncSession, supplier_id: int) -> list[OrdersSyncPlatform]:
    """Стянуть заказы со всех настроенных площадок, сопоставить и сохранить.

    По каждой площадке возвращаем статус: пустой список из-за «нет заказов» и из-за
    «лимит запросов» — разные вещи, и пользователь должен видеть какая именно.
    """
    wh_names = await fourtochki_wh_names(session, supplier_id)
    reports: list[OrdersSyncPlatform] = []

    for platform in MP_PLATFORMS:
        client = await order_client(session, supplier_id, platform)
        if client is None:
            continue  # площадка не настроена — в отчёт не выводим, это не ошибка

        try:
            raw = await client.fbs_orders()
        except (WBError, OzonError) as exc:
            reports.append(OrdersSyncPlatform(platform=platform, ok=False, message=str(exc)))
            continue

        is_test = getattr(client, "sandbox", False)
        fbs_names = await fbs_wh_names(session, supplier_id, platform)

        # Справочники для сопоставления товара и склада — по одной выборке на площадку.
        links = (
            await session.execute(
                select(ProductLink).where(
                    ProductLink.supplier_id == supplier_id,
                    ProductLink.platform == platform,
                )
            )
        ).scalars().all()
        by_chrt = {l.chrt_id: l for l in links if l.chrt_id}
        by_nm = {l.nm_id: l for l in links if l.nm_id}
        by_barcode = {l.barcode: l for l in links if l.barcode}
        by_offer = {l.offer_id: l for l in links if l.offer_id}

        # Привязки складов: fbs_warehouse_id → [(priority, fourtochki_wrh)] по приоритету.
        mapping: dict[str, list[tuple[int, int]]] = {}
        for m in (
            await session.execute(
                select(WarehouseMapping).where(
                    WarehouseMapping.supplier_id == supplier_id,
                    WarehouseMapping.platform == platform,
                )
            )
        ).scalars().all():
            mapping.setdefault(m.fbs_warehouse_id, []).append((m.priority, m.fourtochki_wrh))
        for lst in mapping.values():
            lst.sort()

        product_ids: set[int] = set()
        matched: dict[int, ProductLink] = {}
        for i, o in enumerate(raw):
            link = (
                by_chrt.get(o.get("chrt_id"))
                or by_nm.get(o.get("nm_id"))
                or by_barcode.get(o.get("barcode"))
                or by_offer.get(o.get("offer_id"))
            )
            if link:
                matched[i] = link
                product_ids.add(link.product_id)

        products = (
            {
                p.id: p
                for p in (
                    await session.execute(select(Product).where(Product.id.in_(product_ids)))
                ).scalars().all()
            }
            if product_ids
            else {}
        )

        stock: dict[tuple[int, int], int] = {}
        if product_ids:
            for pid, wrh, rest in (
                await session.execute(
                    select(ProductStock.product_id, ProductStock.wrh, ProductStock.rest).where(
                        ProductStock.product_id.in_(product_ids)
                    )
                )
            ).all():
                stock[(pid, wrh)] = rest

        # Существующие заказы этой площадки — чтобы обновлять, а не плодить дубли.
        existing = {
            o.mp_order_id: o
            for o in (
                await session.execute(
                    select(Order).where(
                        Order.supplier_id == supplier_id, Order.platform == platform
                    )
                )
            ).scalars().all()
        }

        # Актуальные статусы заданий. Отмену иначе не увидеть: отменённое задание
        # пропадает и из /orders/new, и из истории, поэтому спрашиваем статусы не только
        # по выгруженным заказам, но и по всем нашим ещё не закрытым — иначе отмена
        # осталась бы незамеченной навсегда.
        # Best-effort: у Ozon метода нет, а лимитер WB не должен ломать всю выгрузку.
        fetched_ids = {o["mp_order_id"] for o in raw}
        statuses: dict[str, dict] = {}
        if hasattr(client, "order_statuses"):
            ids = fetched_ids | {
                o.mp_order_id
                for o in existing.values()
                if o.supplier_cancelled_at is None and not is_cancelled(o)
            }
            try:
                statuses = await client.order_statuses(
                    [int(i) for i in ids if str(i).isdigit()]
                )
            except (WBError, OzonError) as exc:
                log.warning("Статусы заданий %s недоступны: %s", platform, exc)

        # Заказы, пропавшие из выдачи площадки, но со сменившимся статусом — обновляем
        # на месте: в цикле ниже они не встретятся, там идём по raw.
        for mp_id, st in statuses.items():
            order = existing.get(mp_id)
            if order is not None and mp_id not in fetched_ids:
                order.mp_status = st.get("supplier_status") or order.mp_status
                order.mp_wb_status = st.get("wb_status")
                order.updated_at = datetime.now(UTC)

        for i, o in enumerate(raw):
            link = matched.get(i)
            product = products.get(link.product_id) if link else None
            product_id = link.product_id if link else None

            source_wrh = pick_source(o.get("fbs_warehouse_id"), product_id, mapping, stock)

            item = {
                "cae": product.cae if product else None,
                "name": product.name if product else o.get("article"),
                "qty": o.get("qty") or 1,
                "price": o.get("price"),
                "nm_id": o.get("nm_id"),
                "chrt_id": o.get("chrt_id"),
                "offer_id": o.get("offer_id"),
            }

            order = existing.get(o["mp_order_id"])
            if order is None:
                order = Order(
                    supplier_id=supplier_id, platform=platform, mp_order_id=o["mp_order_id"]
                )
                session.add(order)
                existing[o["mp_order_id"]] = order

            st = statuses.get(str(o["mp_order_id"]))
            # Статус из /orders/status точнее того, что пришло со списком заданий.
            order.mp_status = (st or {}).get("supplier_status") or o.get("mp_status")
            order.mp_wb_status = (st or {}).get("wb_status")
            order.is_test = is_test
            order.fbs_warehouse_id = o.get("fbs_warehouse_id")
            order.fbs_warehouse_name = fbs_names.get(o.get("fbs_warehouse_id") or "")
            order.source_warehouse_id = source_wrh
            order.source_warehouse_name = wh_names.get(source_wrh) if source_wrh else None
            order.items = [item]
            order.updated_at = datetime.now(UTC)

        reports.append(OrdersSyncPlatform(platform=platform, ok=True, fetched=len(raw)))

    await session.commit()
    return reports


# Сколько раз планировщик пробует отменить заказ у поставщика, прежде чем оставить это
# человеку. 4tochki отменяет не всякий заказ, а задача крутится каждые 10 минут.
CANCEL_ATTEMPT_LIMIT = 3

# Отмена приходит либо от продавца (supplierStatus), либо от покупателя/WB (wbStatus).
CANCELLED_SUPPLIER_STATUSES = {"cancel", "cancel_carrier"}
CANCELLED_WB_STATUSES = {
    "canceled",
    "canceled_by_client",
    "declined_by_client",
    "canceled_by_carrier",
    "defect",
}


def is_cancelled(order: Order) -> bool:
    """Отменён ли заказ на стороне площадки."""
    return (
        order.mp_status in CANCELLED_SUPPLIER_STATUSES
        or order.mp_wb_status in CANCELLED_WB_STATUSES
    )


async def cancel_supplier_order(
    session: AsyncSession, supplier_id: int, order: Order
) -> tuple[bool, str]:
    """Отменить у поставщика заказ, отменённый на маркетплейсе.

    Идемпотентно: уже отменённый или ещё не оформленный заказ — не ошибка, просто
    нечего делать. Иначе повторные прогоны планировщика били бы в 4tochki впустую.
    """
    if order.supplier_order_id is None:
        return False, "заказ у поставщика не оформлялся"
    if order.supplier_cancelled_at is not None:
        return False, "уже отменён"

    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.FOURTOCHKI,
            )
        )
    ).scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        return False, "доступы к 4tochki не заданы"
    secrets = load_secrets(cred)

    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        await client.cancel_order(order.supplier_order_id, order.supplier_order_number)
    except (FourTochkiError, KeyError) as exc:
        order.supplier_cancel_attempts += 1
        order.error = str(exc)
        return False, str(exc)

    order.supplier_cancel_attempts += 1
    order.supplier_cancelled_at = datetime.now(UTC)
    order.supplier_status = "тест: отменён" if order.is_test else "отменён"
    order.error = None
    return True, f"№ {order.supplier_order_number or order.supplier_order_id}"


async def place_supplier_order(
    session: AsyncSession, supplier_id: int, order: Order
) -> tuple[bool, str]:
    """Оформить заказ в 4tochki по заказу с маркетплейса — В ТЕСТОВОМ КОНТУРЕ.

    Возвращает (успех, сообщение). Мультисклад: заказ едет на адрес ТОГО FBS-склада,
    откуда пришёл, — адрес берём из привязки склада-источника.
    """
    if order.source_warehouse_id is None:
        return False, "не задан склад-источник (нет привязки к FBS-складу)"

    lines = [
        OrderLine(cae=it["cae"], qty=it.get("qty") or 1, warehouse_id=order.source_warehouse_id)
        for it in order.items
        if it.get("cae")
    ]
    if not lines:
        return False, "позиция не сопоставлена с товаром 4tochki (нет CAE)"

    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.FOURTOCHKI,
            )
        )
    ).scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        return False, "доступы к 4tochki не заданы"
    secrets = load_secrets(cred)

    address_id = await session.scalar(
        select(WarehouseMapping.address_id).where(
            WarehouseMapping.supplier_id == supplier_id,
            WarehouseMapping.platform == order.platform,
            WarehouseMapping.fourtochki_wrh == order.source_warehouse_id,
        )
    ) or (cred.settings or {}).get("address_id")

    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        created = await client.create_order(
            lines, address_id=address_id, order_number=order.mp_order_id, is_test=True
        )
    except (FourTochkiError, KeyError) as exc:
        order.error = str(exc)
        return False, str(exc)

    order.supplier_order_id = created.order_id
    order.supplier_order_number = created.order_number
    order.supplier_status = "тест: принят" if created.success else "тест: ошибка"
    order.error = created.error if not created.success else None
    if not created.success:
        return False, created.error or "4tochki отклонил заказ"
    return True, f"№ {created.order_number or created.order_id}"


async def sync_orders(ctx: dict, supplier_id: int, job_id: int) -> None:
    """Плановая задача: забрать заказы и (если включено) оформить их у поставщика."""
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        job.status = "running"
        await session.commit()

        try:
            reports = await pull_orders(session, supplier_id)
            settings = await get_or_create_settings(session, supplier_id)

            parts = [
                f"{r.platform}: заказов {r.fetched}" if r.ok else f"{r.platform}: {r.message}"
                for r in reports
            ]
            if not reports:
                parts.append("настроенных площадок нет")

            # Отмену прокидываем всегда, независимо от автооформления: заказ уже уехал
            # поставщику, и оставить его висеть после отмены на площадке нельзя.
            cancelled = (
                await session.execute(
                    select(Order).where(
                        Order.supplier_id == supplier_id,
                        Order.supplier_order_id.is_not(None),
                        Order.supplier_cancelled_at.is_(None),
                        Order.supplier_cancel_attempts < CANCEL_ATTEMPT_LIMIT,
                    )
                )
            ).scalars().all()
            undone = cancel_failed = 0
            for order in cancelled:
                if not is_cancelled(order):
                    continue
                ok, msg = await cancel_supplier_order(session, supplier_id, order)
                if ok:
                    undone += 1
                else:
                    cancel_failed += 1
                    log.warning("Заказ %s не отменён у поставщика: %s", order.mp_order_id, msg)
            await session.commit()
            if undone or cancel_failed:
                parts.append(f"отменено в 4tochki: {undone}")
                if cancel_failed:
                    parts.append(f"отмена не удалась: {cancel_failed}")

            placed = failed = 0
            if settings.orders_auto_supplier:
                # Оформляем только те, что ещё не оформлены и у которых есть источник.
                pending = (
                    await session.execute(
                        select(Order).where(
                            Order.supplier_id == supplier_id,
                            Order.supplier_order_id.is_(None),
                            Order.source_warehouse_id.is_not(None),
                        )
                    )
                ).scalars().all()
                for order in pending:
                    # Отменённое на площадке поставщику не отправляем — иначе привезём
                    # товар под заказ, которого уже нет.
                    if is_cancelled(order):
                        continue
                    ok, msg = await place_supplier_order(session, supplier_id, order)
                    if ok:
                        placed += 1
                    else:
                        failed += 1
                        log.warning("Заказ %s не оформлен: %s", order.mp_order_id, msg)
                await session.commit()
                if placed or failed:
                    parts.append(f"оформлено в 4tochki: {placed}")
                    if failed:
                        parts.append(f"не удалось: {failed}")

            job.status = "done"
            job.message = ". ".join(parts) + "."
            job.finished_at = datetime.now(UTC)
            session.add(
                LogEntry(
                    supplier_id=supplier_id,
                    job_id=job_id,
                    level="info",
                    message=job.message,
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
