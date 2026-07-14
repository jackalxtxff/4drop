from decimal import Decimal
from typing import Annotated, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, select

from app.config import get_settings
from app.deps import SessionDep, SupplierDep
from app.models import IntegrationStatus, Product, SyncJob
from app.schemas import (
    IntegrateRequest,
    ProductFacets,
    ProductOut,
    ProductPage,
    SyncJobOut,
)

router = APIRouter(prefix="/suppliers/{supplier_id}/products", tags=["products"])

SortField = Literal["cae", "brand", "min_price", "total_rest"]


def _apply_filters(
    stmt: Select,
    *,
    q: str | None,
    brand: list[str] | None,
    season: list[str] | None,
    goods_type: list[str] | None,
    diameter: list[Decimal] | None,
    in_stock: bool | None,
    price_min: Decimal | None,
    price_max: Decimal | None,
    integration_status: list[str] | None,
) -> Select:
    """Вся фильтрация — на сервере: каталог в десятки тысяч позиций на клиент не выгрузить."""
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            Product.cae.ilike(like)
            | Product.name.ilike(like)
            | Product.brand.ilike(like)
            | Product.model.ilike(like)
        )
    if brand:
        stmt = stmt.where(Product.brand.in_(brand))
    if season:
        stmt = stmt.where(Product.season.in_(season))
    if goods_type:
        stmt = stmt.where(Product.goods_type.in_(goods_type))
    if diameter:
        stmt = stmt.where(Product.diameter.in_(diameter))
    if in_stock is True:
        stmt = stmt.where(Product.total_rest > 0)
    elif in_stock is False:
        stmt = stmt.where(Product.total_rest == 0)
    if price_min is not None:
        stmt = stmt.where(Product.min_price >= price_min)
    if price_max is not None:
        stmt = stmt.where(Product.min_price <= price_max)
    if integration_status:
        stmt = stmt.where(Product.integration_status.in_(integration_status))
    return stmt


@router.get("", response_model=ProductPage)
async def list_products(
    supplier: SupplierDep,
    session: SessionDep,
    q: Annotated[str | None, Query()] = None,
    brand: Annotated[list[str] | None, Query()] = None,
    season: Annotated[list[str] | None, Query()] = None,
    goods_type: Annotated[list[str] | None, Query()] = None,
    diameter: Annotated[list[Decimal] | None, Query()] = None,
    in_stock: Annotated[bool | None, Query()] = None,
    price_min: Annotated[Decimal | None, Query()] = None,
    price_max: Annotated[Decimal | None, Query()] = None,
    integration_status: Annotated[list[str] | None, Query()] = None,
    sort: Annotated[SortField, Query()] = "cae",
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ProductPage:
    filters = dict(
        q=q,
        brand=brand,
        season=season,
        goods_type=goods_type,
        diameter=diameter,
        in_stock=in_stock,
        price_min=price_min,
        price_max=price_max,
        integration_status=integration_status,
    )

    base = _apply_filters(select(Product).where(Product.supplier_id == supplier.id), **filters)

    total = await session.scalar(
        select(func.count()).select_from(base.with_only_columns(Product.id).subquery())
    )

    column = getattr(Product, sort)
    stmt = (
        base.order_by(column.desc() if order == "desc" else column.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return ProductPage(
        items=[ProductOut.model_validate(p) for p in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/facets", response_model=ProductFacets)
async def facets(supplier: SupplierDep, session: SessionDep) -> ProductFacets:
    async def distinct(column) -> list:
        stmt = (
            select(column)
            .where(Product.supplier_id == supplier.id, column.is_not(None))
            .distinct()
            .order_by(column)
        )
        return list((await session.execute(stmt)).scalars().all())

    return ProductFacets(
        brands=await distinct(Product.brand),
        seasons=await distinct(Product.season),
        goods_types=await distinct(Product.goods_type),
        diameters=await distinct(Product.diameter),
    )


async def _enqueue(function: str, **kwargs) -> None:
    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await redis.enqueue_job(function, **kwargs)
    finally:
        await redis.aclose()


@router.post("/sync", response_model=SyncJobOut, status_code=status.HTTP_202_ACCEPTED)
async def sync_catalog(supplier: SupplierDep, session: SessionDep) -> SyncJob:
    """Ручной запуск «Обновить каталог из 4tochki».

    Выгрузка десятков тысяч позиций идёт пачками и занимает минуты, поэтому
    endpoint только ставит задачу и сразу отдаёт job — прогресс UI опрашивает отдельно.
    """
    running = await session.scalar(
        select(func.count())
        .select_from(SyncJob)
        .where(
            SyncJob.supplier_id == supplier.id,
            SyncJob.kind == "catalog",
            SyncJob.status.in_(("queued", "running")),
        )
    )
    if running:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Синхронизация каталога уже идёт для этого поставщика"
        )

    job = SyncJob(supplier_id=supplier.id, kind="catalog", status="queued")
    session.add(job)
    await session.commit()
    await session.refresh(job)

    await _enqueue("sync_catalog", supplier_id=supplier.id, job_id=job.id)
    return job


@router.post("/integrate", response_model=SyncJobOut, status_code=status.HTTP_202_ACCEPTED)
async def integrate(
    payload: IntegrateRequest, supplier: SupplierDep, session: SessionDep
) -> SyncJob:
    """Создание карточек на выбранных площадках для выбранных товаров."""
    unknown = set(payload.platforms) - {"wb", "ozon"}
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Неизвестные площадки: {unknown}")

    if payload.select_all_matching or not payload.product_ids:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Передайте product_ids. Режим «выбрать всё по фильтру» подключается "
            "вместе с созданием карточек.",
        )

    product_ids = list(
        (
            await session.execute(
                select(Product.id).where(
                    Product.supplier_id == supplier.id, Product.id.in_(payload.product_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    if not product_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Товары не найдены у этого поставщика")

    job = SyncJob(
        supplier_id=supplier.id,
        kind="cards",
        status="queued",
        total=len(product_ids) * len(payload.platforms),
        payload={"product_ids": product_ids, "platforms": payload.platforms},
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    await _enqueue("create_cards", supplier_id=supplier.id, job_id=job.id)
    return job


@router.get("/jobs", response_model=list[SyncJobOut])
async def list_jobs(supplier: SupplierDep, session: SessionDep) -> list[SyncJob]:
    stmt = (
        select(SyncJob)
        .where(SyncJob.supplier_id == supplier.id)
        .order_by(SyncJob.started_at.desc())
        .limit(20)
    )
    return list((await session.execute(stmt)).scalars().all())
