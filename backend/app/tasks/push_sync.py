"""Отправка цен и остатков на маркетплейсы.

Реализован Wildberries. Ozon — следующий шаг.

Отправляем только по товарам с активной карточкой (ProductLink.status == active):
у карточки на модерации ещё нет рабочего nmID/баркода, слать остаток некуда.

Цена = закупочная с наценкой (pricing_rules). Остаток = реальный минус буфер
(stock.marketplace_stock) — та же формула, что показывает витрина.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.integrations.wb.client import WBClient, WBError
from app.models import (
    Credential,
    IntegrationStatus,
    LogEntry,
    Platform,
    Product,
    ProductLink,
    ProductStock,
    SyncJob,
    SyncSettings,
    WarehouseMapping,
)
from app.formula import FormulaError, compile_formula, evaluate
from app.security import decrypt_secret
from app.stock import marketplace_stock
from app.tasks.cards_sync import reconcile_wb_pending


def _wh_stock_items(
    rows: list,
    stock_by_prod: dict[int, dict[int, int]],
    bound: list[int],
    is_disabled: bool,
    buffer: int,
) -> list[dict]:
    """Остатки для одного FBS-склада: по штрихкоду, только со связанных складов 4tochki.

    Выключенный склад, заблокированный товар или склад без привязки → остаток 0
    (товар с него не продаётся). Иначе — сумма остатков по привязанным складам 4tochki
    минус буфер. rows — список (ProductLink, Product).

    Остаток адресуется по chrtId (ID размера карточки WB), поэтому связи без chrt_id
    пропускаем: без него позицию не отправить.
    """
    items: list[dict] = []
    for link, product in rows:
        if not link.chrt_id:
            continue
        if is_disabled or product.sync_blocked or not bound:
            amount = 0
        else:
            real = sum(stock_by_prod.get(product.id, {}).get(w, 0) for w in bound)
            amount = marketplace_stock(real, buffer)
        items.append({"chrtId": link.chrt_id, "amount": amount})
    return items


async def _wb_credential(session: AsyncSession, supplier_id: int) -> Credential | None:
    return (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id,
                Credential.platform == Platform.WB,
            )
        )
    ).scalar_one_or_none()


async def push_wb(
    session: AsyncSession, supplier_id: int, job: SyncJob
) -> tuple[str, str]:
    """Возвращает (level, message) для лога и статуса задачи."""
    cred = await _wb_credential(session, supplier_id)
    if cred is None or not cred.secrets_encrypted:
        return "error", "Доступы к Wildberries не заданы"

    api_key = json.loads(decrypt_secret(cred.secrets_encrypted))["api_key"]

    # Перед пушем дорезолвим pending-карточки: проставляем nmID и грузим фото. Иначе
    # карточки, которым WB не присвоил nmID в 10-сек окно создания, навсегда остались бы
    # без остатков (пуш берёт только active) и без фото.
    await reconcile_wb_pending(session, supplier_id, api_key)

    settings = (
        await session.execute(
            select(SyncSettings).where(SyncSettings.supplier_id == supplier_id)
        )
    ).scalar_one_or_none()
    buffer = settings.stock_buffer if settings else 0

    # Формулы WB — цена продажи и цена до скидки, компилируем по разу на пачку.
    try:
        price_formula = compile_formula(settings.wb_price_formula if settings else "purchase")
        before_formula = compile_formula(
            settings.wb_price_before_formula if settings else "price"
        )
    except FormulaError as exc:
        return "error", f"Формула цены WB некорректна: {exc}"

    # Активные карточки WB + их товары.
    rows = (
        await session.execute(
            select(ProductLink, Product)
            .join(Product, Product.id == ProductLink.product_id)
            .where(
                ProductLink.supplier_id == supplier_id,
                ProductLink.platform == Platform.WB,
                ProductLink.status == IntegrationStatus.ACTIVE,
            )
        )
    ).all()

    if not rows:
        return "info", "Нет активных карточек WB — отправлять нечего"

    price_items: list[dict] = []
    no_price = 0

    blocked = 0
    for link, product in rows:
        # Заблокированный товар: цену и атрибуты не трогаем (остаток форсим в 0 ниже,
        # при сборке остатков по складам) — чтобы карточка не продавала снятое, но и
        # не пересоздавалась.
        if product.sync_blocked:
            blocked += 1
            continue

        price = (
            evaluate(price_formula, product.min_price, product.price_rozn, product.weight)
            if product.min_price
            else None
        )

        if link.nm_id and price:
            # Цена до скидки считается от нашей цены (переменная price/wb_price).
            before = evaluate(
                before_formula,
                product.min_price,
                product.price_rozn,
                product.weight,
                price=price,
            )
            # Если формула дала цену до скидки ниже нашей — скидки просто нет.
            before_int = max(int(before or 0), int(price))
            discount = WBClient.discount_percent(before_int, int(price))
            price_items.append(
                {"nmID": link.nm_id, "price": before_int, "discount": discount}
            )
        elif not price:
            no_price += 1

    client = WBClient(api_key)

    # Отправляем только изменившиеся цены: WB валит весь батч, если хоть одна цена
    # в нём совпадает с текущей. Без этого один неизменённый товар блокирует остальные.
    current = await client.current_prices()
    changed = [
        it
        for it in price_items
        if current.get(it["nmID"]) != (it["price"], it["discount"])
    ]
    up_to_date = len(price_items) - len(changed)

    priced, price_err = await client.update_prices(changed)

    # --- остатки по FBS-складам ---------------------------------------------
    # На каждый FBS-склад публикуем остаток ТОЛЬКО тех складов 4tochki, что к нему
    # привязаны (WarehouseMapping). Выключенный склад (settings['fbs_disabled']) —
    # обнуляем. Несвязанный включённый склад не трогаем: источник для него не задан.
    mp_map: dict[str, list[int]] = {}
    for m in (
        await session.execute(
            select(WarehouseMapping).where(
                WarehouseMapping.supplier_id == supplier_id,
                WarehouseMapping.platform == Platform.WB,
            )
        )
    ).scalars():
        mp_map.setdefault(m.fbs_warehouse_id, []).append(m.fourtochki_wrh)

    disabled = set((cred.settings or {}).get("fbs_disabled") or [])
    fbs_names = {
        w["id"]: w.get("name") for w in ((cred.settings or {}).get("fbs_warehouses") or [])
    }

    # Остатки по складам для активных товаров: product_id -> {wrh: rest}.
    active_ids = [p.id for _l, p in rows]
    stock_by_prod: dict[int, dict[int, int]] = {}
    if active_ids:
        for pid, wrh, rest in (
            await session.execute(
                select(ProductStock.product_id, ProductStock.wrh, ProductStock.rest).where(
                    ProductStock.product_id.in_(active_ids)
                )
            )
        ).all():
            stock_by_prod.setdefault(pid, {})[wrh] = rest

    # Обрабатываем склады, у которых есть привязка ИЛИ которые выключены (их обнуляем).
    fbs_ids = set(mp_map) | disabled
    stock_err: str | None = None
    stocked = 0
    stock_notes: list[str] = []
    for fbs_id in sorted(fbs_ids):
        is_disabled = fbs_id in disabled
        bound = mp_map.get(fbs_id, [])
        items = _wh_stock_items(rows, stock_by_prod, bound, is_disabled, buffer)
        if not items:
            continue
        try:
            sent, err = await client.update_stocks(int(fbs_id), items)
            stocked += sent
            name = fbs_names.get(fbs_id, fbs_id)
            stock_notes.append(f"{name}{' (выкл→0)' if is_disabled else ''}: {sent}")
            if err:
                stock_err = err
        except (WBError, ValueError) as exc:
            stock_err = str(exc)

    job.processed = priced + stocked

    env = "песочница WB" if client.sandbox else "боевой кабинет WB"
    priced_msg = f"Цены: обновлено {priced}"
    if up_to_date:
        priced_msg += f", уже актуальны {up_to_date}"
    parts = [f"[{env}] {priced_msg}"]
    if stock_notes:
        parts.append("остатки → " + ", ".join(stock_notes))
    elif not fbs_ids:
        parts.append("остатки: нет привязанных FBS-складов — задайте привязку на «Подключениях»")
    if buffer:
        parts.append(f"буфер {buffer}")
    if blocked:
        parts.append(f"заблокировано (остаток 0): {blocked}")
    if no_price:
        parts.append(f"без цены: {no_price}")
    if price_err:
        parts.append(f"ошибка цен: {price_err}")
    if stock_err:
        parts.append(f"ошибка остатков: {stock_err}")

    # Любая ошибка отправки (цен или остатков) помечает задачу как проблемную,
    # даже если часть прошла: иначе провал остатков теряется под статусом «готово».
    # Норму — «всё уже актуально, менять нечего» — ошибкой не считаем.
    has_error = bool(price_err or stock_err)
    level = "error" if has_error else "info"
    return level, ". ".join(parts)


async def push_marketplaces(ctx: dict, supplier_id: int, job_id: int) -> None:
    async with SessionLocal() as session:
        job = await session.get(SyncJob, job_id)
        if job is None:
            return

        job.status = "running"
        await session.commit()

        try:
            level, message = await push_wb(session, supplier_id, job)

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
