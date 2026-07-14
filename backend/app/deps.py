from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Supplier, User
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация")

    user_id = decode_access_token(creds.credentials)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недействительный токен")

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь недоступен")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_supplier(
    supplier_id: Annotated[int, Path()],
    user: CurrentUser,
    session: SessionDep,
) -> Supplier:
    """Поставщик всегда достаётся с проверкой владельца.

    Данные поставщиков изолированы друг от друга; без этой проверки любой
    авторизованный пользователь читал бы чужой каталог и доступы.
    """
    result = await session.execute(
        select(Supplier).where(Supplier.id == supplier_id, Supplier.owner_id == user.id)
    )
    supplier = result.scalar_one_or_none()
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поставщик не найден")
    return supplier


SupplierDep = Annotated[Supplier, Depends(get_supplier)]
