from fastapi import APIRouter, status
from sqlalchemy import func, select

from app.deps import CurrentUser, SessionDep, SupplierDep
from app.models import Product, Supplier
from app.schemas import SupplierCreate, SupplierOut, SupplierUpdate

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


@router.get("", response_model=list[SupplierOut])
async def list_suppliers(user: CurrentUser, session: SessionDep) -> list[SupplierOut]:
    counts = (
        select(Product.supplier_id, func.count(Product.id).label("n"))
        .group_by(Product.supplier_id)
        .subquery()
    )
    result = await session.execute(
        select(Supplier, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.supplier_id == Supplier.id)
        .where(Supplier.owner_id == user.id)
        .order_by(Supplier.created_at)
    )
    return [
        SupplierOut(**SupplierOut.model_validate(s).model_dump() | {"product_count": n})
        for s, n in result.all()
    ]


@router.post("", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
async def create_supplier(
    payload: SupplierCreate, user: CurrentUser, session: SessionDep
) -> Supplier:
    supplier = Supplier(owner_id=user.id, name=payload.name, comment=payload.comment)
    session.add(supplier)
    await session.commit()
    await session.refresh(supplier)
    return supplier


@router.patch("/{supplier_id}", response_model=SupplierOut)
async def update_supplier(
    payload: SupplierUpdate, supplier: SupplierDep, session: SessionDep
) -> Supplier:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    await session.commit()
    await session.refresh(supplier)
    return supplier


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_supplier(supplier: SupplierDep, session: SessionDep) -> None:
    # Каскад уносит доступы, каталог, маппинги и заказы этого поставщика.
    await session.delete(supplier)
    await session.commit()
