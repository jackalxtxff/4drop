import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.deps import SessionDep, SupplierDep
from app.integrations.fourtochki.client import FourTochkiClient, FourTochkiError
from app.integrations.ozon.client import OzonClient
from app.integrations.wb.client import WBClient
from app.models import ConnectionStatus, Credential, Platform
from app.schemas import (
    CredentialOut,
    FourTochkiCredentialIn,
    OzonCredentialIn,
    WarehouseOut,
    WBCredentialIn,
)
from app.security import decrypt_secret, encrypt_secret, mask_secret
from app.tasks.catalog_sync import recompute_aggregates

router = APIRouter(prefix="/suppliers/{supplier_id}/connections", tags=["connections"])


async def _get_or_create(session: SessionDep, supplier_id: int, platform: str) -> Credential:
    result = await session.execute(
        select(Credential).where(
            Credential.supplier_id == supplier_id, Credential.platform == platform
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        cred = Credential(supplier_id=supplier_id, platform=platform)
        session.add(cred)
    return cred


async def _require(session: SessionDep, supplier_id: int, platform: str) -> Credential:
    result = await session.execute(
        select(Credential).where(
            Credential.supplier_id == supplier_id, Credential.platform == platform
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None or not cred.secrets_encrypted:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Доступы к {platform} для этого поставщика не заданы"
        )
    return cred


def load_secrets(cred: Credential) -> dict[str, str]:
    """Расшифровка происходит только в памяти и никогда не уходит в ответ или лог."""
    if not cred.secrets_encrypted:
        return {}
    return json.loads(decrypt_secret(cred.secrets_encrypted))


def _store_secrets(cred: Credential, secrets: dict[str, str]) -> None:
    clean = {k: v for k, v in secrets.items() if v}
    cred.secrets_encrypted = encrypt_secret(json.dumps(clean))
    cred.secrets_masked = {k: mask_secret(v) for k, v in clean.items() if k != "login"}
    if "login" in clean:
        cred.secrets_masked["login"] = clean["login"]  # логин не секрет, показываем целиком


@router.get("", response_model=list[CredentialOut])
async def list_connections(supplier: SupplierDep, session: SessionDep) -> list[CredentialOut]:
    result = await session.execute(
        select(Credential).where(Credential.supplier_id == supplier.id)
    )
    existing = {c.platform: c for c in result.scalars()}

    out = []
    for platform in (Platform.FOURTOCHKI, Platform.WB, Platform.OZON):
        cred = existing.get(platform)
        if cred is None:
            out.append(
                CredentialOut(
                    platform=platform,
                    status=ConnectionStatus.NOT_CONFIGURED,
                    status_message=None,
                    checked_at=None,
                    secrets_masked={},
                )
            )
        else:
            out.append(CredentialOut.model_validate(cred))
    return out


@router.put("/fourtochki", response_model=CredentialOut)
async def set_fourtochki(
    payload: FourTochkiCredentialIn, supplier: SupplierDep, session: SessionDep
) -> Credential:
    cred = await _get_or_create(session, supplier.id, Platform.FOURTOCHKI)
    _store_secrets(cred, {"login": payload.login, "password": payload.password})
    cred.selected_warehouses = payload.selected_warehouses
    cred.status = ConnectionStatus.NOT_CONFIGURED
    cred.status_message = "Доступы сохранены, подключение не проверено"
    await session.commit()
    await session.refresh(cred)
    return cred


@router.put("/wb", response_model=CredentialOut)
async def set_wb(
    payload: WBCredentialIn, supplier: SupplierDep, session: SessionDep
) -> Credential:
    cred = await _get_or_create(session, supplier.id, Platform.WB)
    _store_secrets(cred, {"api_key": payload.api_key})
    cred.status = ConnectionStatus.NOT_CONFIGURED
    cred.status_message = "Ключи сохранены, подключение не проверено"
    await session.commit()
    await session.refresh(cred)
    return cred


@router.put("/ozon", response_model=CredentialOut)
async def set_ozon(
    payload: OzonCredentialIn, supplier: SupplierDep, session: SessionDep
) -> Credential:
    cred = await _get_or_create(session, supplier.id, Platform.OZON)
    _store_secrets(cred, {"client_id": payload.client_id, "api_key": payload.api_key})
    cred.status = ConnectionStatus.NOT_CONFIGURED
    cred.status_message = "Доступы сохранены, подключение не проверено"
    await session.commit()
    await session.refresh(cred)
    return cred


@router.post("/fourtochki/check", response_model=CredentialOut)
async def check_fourtochki(supplier: SupplierDep, session: SessionDep) -> Credential:
    """Ping(login, password) → bool, затем сразу тянем справочник складов.

    Склады подтягиваются в тот же заход: без них пользователь не сможет выбрать,
    с каких складов брать остатки, а без этого выбора синхронизация бессмысленна.
    """
    cred = await _require(session, supplier.id, Platform.FOURTOCHKI)
    secrets = load_secrets(cred)

    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        if not await client.ping():
            raise FourTochkiError("Ping вернул False — логин или пароль не приняты")
        account = await client.get_account_name()
        warehouses = await client.get_warehouses()
    except (FourTochkiError, KeyError) as exc:
        cred.status = ConnectionStatus.ERROR
        cred.status_message = str(exc)
        cred.checked_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(cred)
        return cred

    cred.warehouses = [
        WarehouseOut(
            id=w.id,
            name=w.name,
            short_name=w.short_name,
            logistic_days=w.logistic_days,
            have_delivery=w.have_delivery,
            is_paid_delivery=w.is_paid_delivery,
        ).model_dump()
        for w in warehouses
    ]
    # Если пользователь ещё ничего не выбрал, не выбираем за него: молча включить
    # все склады — значит опубликовать остатки складов с долгой логистикой и сорвать SLA.
    cred.status = ConnectionStatus.OK
    cred.account_name = account
    cred.status_message = f"Подключение работает, складов доступно: {len(warehouses)}"
    cred.checked_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(cred)
    return cred


@router.post("/wb/check", response_model=CredentialOut)
async def check_wb(supplier: SupplierDep, session: SessionDep) -> Credential:
    cred = await _require(session, supplier.id, Platform.WB)
    secrets = load_secrets(cred)

    client = WBClient(secrets["api_key"])
    ok, message = await client.check()

    cred.status = ConnectionStatus.OK if ok else ConnectionStatus.ERROR
    cred.status_message = message
    cred.account_name = await client.seller_name() if ok else None
    cred.checked_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(cred)
    return cred


@router.post("/ozon/check", response_model=CredentialOut)
async def check_ozon(supplier: SupplierDep, session: SessionDep) -> Credential:
    cred = await _require(session, supplier.id, Platform.OZON)
    secrets = load_secrets(cred)

    client = OzonClient(secrets["client_id"], secrets["api_key"])
    ok, message = await client.check()

    cred.status = ConnectionStatus.OK if ok else ConnectionStatus.ERROR
    cred.status_message = message
    cred.account_name = await client.seller_name() if ok else None
    cred.checked_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(cred)
    return cred


@router.put("/fourtochki/warehouses", response_model=CredentialOut)
async def set_warehouses(
    warehouse_ids: list[int], supplier: SupplierDep, session: SessionDep
) -> Credential:
    """Смена набора складов не требует перевыкачки каталога.

    Цены и остатки лежат в product_stocks по складам, поэтому достаточно пересчитать
    агрегаты (total_rest, min_price) — это один UPDATE, делаем его сразу здесь.
    Откладывать пересчёт до следующей синхронизации нельзя: пользователь отметил бы
    склады и не увидел никакого эффекта, а каталог продолжал бы показывать нули.
    """
    cred = await _require(session, supplier.id, Platform.FOURTOCHKI)
    known = {w["id"] for w in cred.warehouses}
    unknown = set(warehouse_ids) - known
    if unknown:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Склады не найдены в справочнике 4tochki: {sorted(unknown)}",
        )

    cred.selected_warehouses = warehouse_ids
    await recompute_aggregates(session, supplier.id, warehouse_ids)
    await session.commit()
    await session.refresh(cred)
    return cred
