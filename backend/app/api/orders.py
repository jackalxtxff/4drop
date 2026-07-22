"""Заказы с маркетплейсов.

Сценарий FBS: заказ приходит на FBS-склад маркетплейса, а товар физически лежит на
складах 4tochki. Здесь мы:
  * тянем текущие заказы с площадок (WB — полноценно через песочницу, Ozon — основа);
  * сопоставляем позицию заказа с нашим товаром (по nmId/chrtId/штрихкоду/артикулу);
  * по привязке складов определяем, из какого склада 4tochki заказ поедет;
  * умеем оформить заказ в 4tochki в ТЕСТОВОМ контуре (CreateOrder is_test=True).

Саму привязку складов 4tochki к FBS-складам настраивают на странице «Подключения»
(см. connections.py) — здесь мы её только читаем. Всё создание заказов идёт через
тестовые контуры — реальных отгрузок не происходит.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import SessionDep, SupplierDep
from app.integrations.fourtochki.client import FourTochkiClient, FourTochkiError, OrderLine
from app.integrations.ozon.client import OzonError
from app.integrations.wb.client import WBError
from app.models import (
    Credential,
    Order,
    Platform,
    Product,
    ProductLink,
    ProductStock,
    WarehouseMapping,
)
from app.schemas import (
    OrderOut,
    OrdersSyncPlatform,
    OrdersSyncResult,
)
from app.api.connections import _mp_client, load_secrets

router = APIRouter(prefix="/suppliers/{supplier_id}/orders", tags=["orders"])

# Площадки, с которых умеем тянуть заказы. Ozon — основа (без песочницы).
MP_PLATFORMS = (Platform.WB, Platform.OZON)


# --- вспомогательное ---------------------------------------------------------


async def _fourtochki_wh_names(session: SessionDep, supplier_id: int) -> dict[int, str]:
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


async def _order_client(session: SessionDep, supplier_id: int, platform: str):
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


def _pick_source(
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


# --- заказы ------------------------------------------------------------------


@router.get("", response_model=list[OrderOut])
async def list_orders(supplier: SupplierDep, session: SessionDep) -> list[Order]:
    rows = (
        await session.execute(
            select(Order)
            .where(Order.supplier_id == supplier.id)
            .order_by(Order.created_at.desc())
            .limit(500)
        )
    ).scalars().all()
    return list(rows)


@router.post("/sync", response_model=OrdersSyncResult)
async def sync_orders(supplier: SupplierDep, session: SessionDep) -> OrdersSyncResult:
    """Стянуть заказы со всех настроенных площадок и сохранить их.

    Идёт через тестовые контуры площадок (для WB — песочница по флагу в токене).
    Сопоставляет позиции с нашими товарами и проставляет склад-источник 4tochki.
    По каждой площадке возвращаем статус: пустой список из-за «нет заказов» и из-за
    «лимит запросов» — разные вещи, и пользователь должен видеть какая именно.
    """
    wh_names = await _fourtochki_wh_names(session, supplier.id)
    reports: list[OrdersSyncPlatform] = []

    for platform in MP_PLATFORMS:
        client = await _order_client(session, supplier.id, platform)
        if client is None:
            continue  # площадка не настроена — в отчёт не выводим, это не ошибка

        try:
            raw = await client.fbs_orders()
        except (WBError, OzonError) as exc:
            reports.append(
                OrdersSyncPlatform(platform=platform, ok=False, message=str(exc))
            )
            continue

        is_test = getattr(client, "sandbox", False)

        # Справочники для сопоставления товара и склада — по одной выборке на площадку.
        links = (
            await session.execute(
                select(ProductLink).where(
                    ProductLink.supplier_id == supplier.id,
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
                    WarehouseMapping.supplier_id == supplier.id,
                    WarehouseMapping.platform == platform,
                )
            )
        ).scalars().all():
            mapping.setdefault(m.fbs_warehouse_id, []).append((m.priority, m.fourtochki_wrh))
        for lst in mapping.values():
            lst.sort()

        # Остатки по товарам, попавшим в заказы, — одной выборкой.
        product_ids = set()
        matched: dict[int, ProductLink] = {}  # index в raw → link
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

        products = {
            p.id: p
            for p in (
                await session.execute(
                    select(Product).where(Product.id.in_(product_ids))
                )
            ).scalars().all()
        } if product_ids else {}

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
                        Order.supplier_id == supplier.id, Order.platform == platform
                    )
                )
            ).scalars().all()
        }

        for i, o in enumerate(raw):
            link = matched.get(i)
            product = products.get(link.product_id) if link else None
            product_id = link.product_id if link else None

            source_wrh = _pick_source(o.get("fbs_warehouse_id"), product_id, mapping, stock)

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
                    supplier_id=supplier.id,
                    platform=platform,
                    mp_order_id=o["mp_order_id"],
                )
                session.add(order)
                existing[o["mp_order_id"]] = order

            order.mp_status = o.get("mp_status")
            order.is_test = is_test
            order.fbs_warehouse_id = o.get("fbs_warehouse_id")
            order.source_warehouse_id = source_wrh
            order.source_warehouse_name = wh_names.get(source_wrh) if source_wrh else None
            order.items = [item]
            order.updated_at = datetime.now(UTC)

        reports.append(
            OrdersSyncPlatform(platform=platform, ok=True, fetched=len(raw))
        )

    await session.commit()
    orders = await list_orders(supplier, session)
    return OrdersSyncResult(
        orders=[OrderOut.model_validate(o) for o in orders], platforms=reports
    )


@router.post("/{order_id}/supplier-order", response_model=OrderOut)
async def create_supplier_order(
    order_id: int, supplier: SupplierDep, session: SessionDep
) -> Order:
    """Оформить заказ в 4tochki по заказу с маркетплейса — В ТЕСТОВОМ КОНТУРЕ.

    CreateOrder(is_test=True): 4tochki принимает заказ, но реальной отгрузки нет.
    Берём склад-источник, определённый при синхронизации (source_warehouse_id).
    """
    order = await session.get(Order, order_id)
    if order is None or order.supplier_id != supplier.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заказ не найден")
    if order.source_warehouse_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Не задан склад-источник: привяжите склад 4tochki к FBS-складу этого заказа",
        )

    lines = [
        OrderLine(cae=it["cae"], qty=it.get("qty") or 1, warehouse_id=order.source_warehouse_id)
        for it in order.items
        if it.get("cae")
    ]
    if not lines:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Позиция заказа не сопоставлена с товаром 4tochki (нет CAE)",
        )

    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier.id,
                Credential.platform == Platform.FOURTOCHKI,
            )
        )
    ).scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Доступы к 4tochki не заданы")
    secrets = load_secrets(cred)

    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        created = await client.create_order(
            lines, order_number=order.mp_order_id, is_test=True
        )
    except (FourTochkiError, KeyError) as exc:
        order.error = str(exc)
        await session.commit()
        await session.refresh(order)
        return order

    order.supplier_order_id = created.order_id
    order.supplier_order_number = created.order_number
    order.supplier_status = "тест: принят" if created.success else "тест: ошибка"
    order.error = created.error if not created.success else None
    await session.commit()
    await session.refresh(order)
    return order

