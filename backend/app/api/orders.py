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

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import SessionDep, SupplierDep
from app.integrations.wb.client import WBError
from app.models import Order, Platform
from app.schemas import OrderOut, OrdersSyncResult
from app.tasks.orders_sync import (
    cancel_supplier_order,
    order_client,
    place_supplier_order,
    pull_orders,
)

router = APIRouter(prefix="/suppliers/{supplier_id}/orders", tags=["orders"])


# Ядро (выгрузка заказов, выбор склада-источника, оформление у поставщика) живёт в
# app/tasks/orders_sync.py — им пользуются и эти эндпоинты, и плановая задача.


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
    """Ручная синхронизация заказов (та же логика, что у плановой задачи)."""
    reports = await pull_orders(session, supplier.id)
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
    Та же логика, что у плановой задачи (см. tasks/orders_sync.place_supplier_order).
    """
    order = await session.get(Order, order_id)
    if order is None or order.supplier_id != supplier.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заказ не найден")

    ok, message = await place_supplier_order(session, supplier.id, order)
    await session.commit()
    await session.refresh(order)
    if not ok and order.supplier_order_id is None and order.error is None:
        # Причина не про сам заказ (нет привязки/CAE/доступов) — отвечаем понятной 400.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message)
    return order


async def _order_or_404(order_id: int, supplier: SupplierDep, session: SessionDep) -> Order:
    order = await session.get(Order, order_id)
    if order is None or order.supplier_id != supplier.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Заказ не найден")
    return order


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(order_id: int, supplier: SupplierDep, session: SessionDep) -> Order:
    """Отменить сборочное задание на площадке и заказ у поставщика.

    Порядок важен: сначала площадка. Если она отмену запретила (задание уже передано
    WB — 409), заказ поставщику нужен, и отменять его нельзя.
    """
    order = await _order_or_404(order_id, supplier, session)

    client = await order_client(session, supplier.id, order.platform)
    if client is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Доступы к площадке не заданы")
    if not hasattr(client, "cancel_order"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Отмена заказа для этой площадки не поддерживается"
        )

    try:
        await client.cancel_order(int(order.mp_order_id))
    except (WBError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    order.mp_status = "cancel"
    ok, message = await cancel_supplier_order(session, supplier.id, order)
    await session.commit()
    await session.refresh(order)
    # Заказа у поставщика могло и не быть — это не ошибка отмены на площадке.
    if not ok and order.supplier_order_id is not None and order.supplier_cancelled_at is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Задание отменено, но 4tochki не отменил: {message}"
        )
    return order


@router.post("/{order_id}/test-decline", response_model=OrderOut)
async def test_decline_order(order_id: int, supplier: SupplierDep, session: SessionDep) -> Order:
    """Песочница: эмулировать отмену заказа покупателем.

    Тестовый контур WB. Нужно, чтобы проверить сквозной сценарий отмены, не дожидаясь
    реального покупателя. Сама отмена у поставщика прокинется синхронизацией заказов.
    """
    order = await _order_or_404(order_id, supplier, session)
    if order.platform != Platform.WB:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Доступно только для Wildberries")

    client = await order_client(session, supplier.id, order.platform)
    if client is None or not getattr(client, "sandbox", False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нужен ключ песочницы WB")

    try:
        await client.test_decline_order(int(order.mp_order_id))
    except (WBError, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await pull_orders(session, supplier.id)
    await session.refresh(order)
    return order
