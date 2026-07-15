from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.config import get_settings
from app.deps import SessionDep, SupplierDep
from app.models import SyncJob, SyncSettings
from app.formula import FormulaError, compile_formula, price_from_formula
from app.schemas import (
    FormulaPreviewIn,
    FormulaPreviewOut,
    SyncJobOut,
    SyncSettingsIn,
    SyncSettingsOut,
)
from app.tasks.catalog_sync import get_or_create_settings

router = APIRouter(prefix="/suppliers/{supplier_id}/sync", tags=["sync"])

# kind → функция воркера. Тот же справочник, что и у планировщика.
KINDS = {
    "catalog": "sync_catalog",
    "stocks": "sync_stocks",
    "push": "push_marketplaces",
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
    for field, value in payload.model_dump().items():
        setattr(settings, field, value)
    await session.commit()
    await session.refresh(settings)
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


@router.get("/jobs", response_model=list[SyncJobOut])
async def list_jobs(supplier: SupplierDep, session: SessionDep) -> list[SyncJob]:
    stmt = (
        select(SyncJob)
        .where(SyncJob.supplier_id == supplier.id)
        .order_by(SyncJob.started_at.desc())
        .limit(50)
    )
    return list((await session.execute(stmt)).scalars().all())
