from arq import create_pool
from arq.connections import RedisSettings
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import SessionDep, SupplierDep
from app.models import SyncJob, SyncSettings
from app.formula import FormulaError, compile_formula, price_from_formula
from app.schemas import (
    FormulaPreviewIn,
    FormulaPreviewOut,
    SyncJobOut,
    SyncJobPage,
    SyncSettingsIn,
    SyncSettingsOut,
)
from app.tasks.catalog_sync import get_or_create_settings
from app.tasks.enqueue import enqueue_kind

router = APIRouter(prefix="/suppliers/{supplier_id}/sync", tags=["sync"])

# kind → функция воркера. Тот же справочник, что и у планировщика.
KINDS = {
    "catalog": "sync_catalog",
    "stocks": "sync_stocks",
    "push": "push_marketplaces",
    "cards_update": "update_cards",
    "auto_cards": "auto_cards",
}


@router.get("/settings", response_model=SyncSettingsOut)
async def get_sync_settings(supplier: SupplierDep, session: SessionDep) -> SyncSettings:
    return await get_or_create_settings(session, supplier.id)


@router.put("/settings", response_model=SyncSettingsOut)
async def set_sync_settings(
    payload: SyncSettingsIn, supplier: SupplierDep, session: SessionDep
) -> SyncSettings:
    # Формулы проверяем до сохранения: битая формула в настройках сломала бы весь
    # пуш цен молча — лучше отклонить её здесь с понятным текстом.
    for field, label in (
        ("wb_price_formula", "Wildberries — цена"),
        ("ozon_price_formula", "Ozon — цена"),
        ("wb_price_before_formula", "Wildberries — цена до скидки"),
        ("ozon_price_before_formula", "Ozon — цена до скидки"),
    ):
        try:
            compile_formula(getattr(payload, field))
        except FormulaError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Формула цены {label}: {exc}"
            )

    settings = await get_or_create_settings(session, supplier.id)

    # Что поменялось — чтобы решить, нужен ли немедленный пуш. Цена и буфер влияют
    # на то, что уже стоит на витрине, поэтому применяем сразу, не дожидаясь расписания.
    price_before = (
        settings.wb_price_formula,
        settings.ozon_price_formula,
        settings.wb_price_before_formula,
        settings.ozon_price_before_formula,
    )
    buffer_before = settings.stock_buffer
    prefix_before = settings.vendor_prefix

    for field, value in payload.model_dump().items():
        setattr(settings, field, value)

    # Прежний префикс запоминаем: карточки, созданные под ним, должны остаться «своими»,
    # иначе система перестанет их узнавать и заведёт дубли с новым артикулом.
    if settings.vendor_prefix != prefix_before:
        history = list(settings.vendor_prefix_history or [])
        if prefix_before not in history:
            history.append(prefix_before)
        settings.vendor_prefix_history = [p for p in history if p != settings.vendor_prefix]

    await session.commit()
    await session.refresh(settings)

    price_changed = price_before != (
        settings.wb_price_formula,
        settings.ozon_price_formula,
        settings.wb_price_before_formula,
        settings.ozon_price_before_formula,
    )
    if price_changed or settings.stock_buffer != buffer_before:
        await enqueue_kind(session, supplier.id, "push")

    return settings


@router.post("/settings/preview-formula", response_model=FormulaPreviewOut)
async def preview_formula(
    payload: FormulaPreviewIn, supplier: SupplierDep, session: SessionDep
) -> FormulaPreviewOut:
    """Посчитать формулу на заданных числах — для живого предпросмотра в UI."""
    try:
        price = price_from_formula(
            payload.formula, payload.purchase, payload.rrp, payload.weight, payload.price
        )
        return FormulaPreviewOut(ok=True, price=price)
    except FormulaError as exc:
        return FormulaPreviewOut(ok=False, error=str(exc))


@router.post("/run/{kind}", response_model=SyncJobOut, status_code=status.HTTP_202_ACCEPTED)
async def run_now(kind: str, supplier: SupplierDep, session: SessionDep) -> SyncJob:
    """Ручной запуск задачи вне расписания."""
    if kind not in KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Неизвестная задача: {kind}"
        )

    running = await session.scalar(
        select(func.count())
        .select_from(SyncJob)
        .where(
            SyncJob.supplier_id == supplier.id,
            SyncJob.kind == kind,
            SyncJob.status.in_(("queued", "running")),
        )
    )
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, "Эта задача уже выполняется")

    job = SyncJob(supplier_id=supplier.id, kind=kind, status="queued")
    session.add(job)
    await session.commit()
    await session.refresh(job)

    redis = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await redis.enqueue_job(KINDS[kind], supplier_id=supplier.id, job_id=job.id)
    finally:
        await redis.aclose()

    return job


@router.get("/jobs", response_model=SyncJobPage)
async def list_jobs(
    supplier: SupplierDep,
    session: SessionDep,
    kind: Annotated[str | None, Query()] = None,
    job_status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SyncJobPage:
    """Журнал задач с пагинацией и фильтрами по типу и статусу."""
    base = select(SyncJob).where(SyncJob.supplier_id == supplier.id)
    if kind:
        base = base.where(SyncJob.kind == kind)
    if job_status:
        base = base.where(SyncJob.status == job_status)

    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )
    rows = (
        await session.execute(
            base.order_by(SyncJob.started_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()

    return SyncJobPage(
        items=[SyncJobOut.model_validate(j) for j in rows],
        total=total or 0,
        offset=offset,
        limit=limit,
    )
