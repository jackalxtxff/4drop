from decimal import Decimal
from typing import Annotated, Literal

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, select

from app.config import get_settings
from app.deps import SessionDep, SupplierDep
from app.models import (
    Credential,
    IntegrationStatus,
    Platform,
    Product,
    ProductStock,
    SyncJob,
)
from app.schemas import (
    IntegrateRequest,
    ProductFacets,
    ProductOut,
    ProductPage,
    ProductStockOut,
    SyncJobOut,
)

router = APIRouter(prefix="/suppliers/{supplier_id}/products", tags=["products"])

SortField = Literal[
    "cae", "brand", "model", "name", "season",
    "width", "height", "diameter",
    "tyre_type", "constr", "camera", "noise",
    "total_rest", "min_price", "integration_status",
]


def _apply_filters(
    stmt: Select,
    *,
    q: str | None,
    brand: list[str] | None,
    season: list[str] | None,
    goods_type: list[str] | None,
    diameter: list[Decimal] | None,
    width: list[Decimal] | None,
    height: list[Decimal] | None,
    tyre_type: list[str] | None,
    constr: list[str] | None,
    camera: list[str] | None,
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
    if width:
        stmt = stmt.where(Product.width.in_(width))
    if height:
        stmt = stmt.where(Product.height.in_(height))
    if tyre_type:
        stmt = stmt.where(Product.tyre_type.in_(tyre_type))
    if constr:
        stmt = stmt.where(Product.constr.in_(constr))
    if camera:
        stmt = stmt.where(Product.camera.in_(camera))
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
    width: Annotated[list[Decimal] | None, Query()] = None,
    height: Annotated[list[Decimal] | None, Query()] = None,
    tyre_type: Annotated[list[str] | None, Query()] = None,
    constr: Annotated[list[str] | None, Query()] = None,
    camera: Annotated[list[str] | None, Query()] = None,
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
        width=width,
        height=height,
        tyre_type=tyre_type,
        constr=constr,
        camera=camera,
        in_stock=in_stock,
        price_min=price_min,
        price_max=price_max,
        integration_status=integration_status,
    )

    base = _apply_filters(select(Product).where(Product.supplier_id == supplier.id), **filters)

    # Счётчик и сводку берём одним проходом по отфильтрованной выборке:
    # три отдельных агрегата по каталогу в 22k строк — три одинаковых скана.
    sub = base.with_only_columns(Product.id, Product.total_rest).subquery()
    total, in_stock_count, total_rest = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(sub.c.total_rest > 0),
                func.coalesce(func.sum(sub.c.total_rest), 0),
            ).select_from(sub)
        )
    ).one()

    column = getattr(Product, sort)
    direction = column.desc() if order == "desc" else column.asc()

    stmt = (
        # NULLS LAST: товары без бренда или цены не должны занимать первую страницу.
        # Product.id — тайбрейкер: сортировка по колонке с повторами (бренд, сезон)
        # без него даёт нестабильный порядок, и строки дублируются между страницами.
        base.order_by(direction.nullslast(), Product.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return ProductPage(
        items=[ProductOut.model_validate(p) for p in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
        in_stock_count=in_stock_count or 0,
        total_rest=total_rest or 0,
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
        widths=await distinct(Product.width),
        heights=await distinct(Product.height),
        tyre_types=await distinct(Product.tyre_type),
        constrs=await distinct(Product.constr),
        cameras=await distinct(Product.camera),
    )


@router.get("/{product_id}/stocks", response_model=list[ProductStockOut])
async def product_stocks(
    product_id: int, supplier: SupplierDep, session: SessionDep
) -> list[ProductStockOut]:
    """Остатки одного товара в разрезе складов — для подсказки над колонкой «Остаток».

    Отдельным запросом, а не внутри списка: класть склады в каждую из 200 строк
    страницы значило бы раздувать ответ ради данных, которые нужны для одной
    строки под курсором.

    Возвращаем ВСЕ склады, включая невыбранные (`selected: false`): если товар лежит
    на складе, который не отмечен в «Подключениях», это надо видеть — иначе непонятно,
    почему в колонке ноль, хотя товар у поставщика есть.
    """
    product = await session.get(Product, product_id)
    if product is None or product.supplier_id != supplier.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Товар не найден у этого поставщика")

    cred = (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier.id,
                Credential.platform == Platform.FOURTOCHKI,
            )
        )
    ).scalar_one_or_none()

    names = {w["id"]: w.get("name") for w in (cred.warehouses if cred else [])}
    days = {w["id"]: w.get("logistic_days") for w in (cred.warehouses if cred else [])}
    selected = set(cred.selected_warehouses if cred else [])

    rows = (
        (
            await session.execute(
                select(ProductStock)
                .where(ProductStock.product_id == product_id, ProductStock.rest > 0)
                .order_by(ProductStock.rest.desc())
            )
        )
        .scalars()
        .all()
    )

    return [
        ProductStockOut(
            wrh=r.wrh,
            name=names.get(r.wrh),
            rest=r.rest,
            price=r.price,
            logistic_days=days.get(r.wrh),
            selected=r.wrh in selected,
        )
        for r in rows
    ]


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
