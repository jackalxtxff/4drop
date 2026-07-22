import json
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, func, select

from app.deps import SessionDep, SupplierDep
from app.integrations.fourtochki.client import FourTochkiClient, FourTochkiError
from app.integrations.ozon.client import OzonClient, OzonError
from app.integrations.wb.client import WBClient, WBError
from app.models import (
    ConnectionStatus,
    Credential,
    Platform,
    Product,
    ProductStock,
    WarehouseMapping,
)
from app.schemas import (
    CredentialOut,
    FbsWarehouseOut,
    FourTochkiCredentialIn,
    OzonCredentialIn,
    PlatformMappingView,
    WarehouseMappingOut,
    WarehouseMappingsIn,
    WarehouseMappingsView,
    WarehouseOut,
    WBCredentialIn,
)
from app.security import decrypt_secret, encrypt_secret, mask_secret
from app.tasks.catalog_sync import recompute_aggregates
from app.tasks.enqueue import enqueue_kind

router = APIRouter(prefix="/suppliers/{supplier_id}/connections", tags=["connections"])

# Площадки маркетплейсов, к FBS-складам которых привязываем склады 4tochki.
MP_PLATFORMS = (Platform.WB, Platform.OZON)


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


def _mp_client(platform: str, secrets: dict[str, str]):
    """Клиент площадки маркетплейса по её секретам, или None для неизвестной."""
    if platform == Platform.WB:
        return WBClient(secrets["api_key"])
    if platform == Platform.OZON:
        return OzonClient(secrets["client_id"], secrets["api_key"])
    return None


async def _refresh_fbs_cache(cred: Credential) -> tuple[list[dict], str | None]:
    """Стянуть FBS-склады площадки и сохранить их в cred.settings['fbs_warehouses'].

    FBS-склады храним в БД, чтобы UI привязки не дёргал лимитируемый API площадки на
    каждой отрисовке (WB-песочница отвечает 429 по глобальному лимитеру). Возвращает
    (склады, ошибка|None); при успехе кэш в cred обновлён (коммит — на вызывающем),
    при ошибке отдаём ранее сохранённый список.
    """
    client = _mp_client(cred.platform, load_secrets(cred))
    if client is None:
        return [], "Неизвестная площадка"
    try:
        whs = await client.list_fbs_warehouses()
    except (WBError, OzonError) as exc:
        cached = (cred.settings or {}).get("fbs_warehouses") or []
        return [{"id": w["id"], "name": w.get("name")} for w in cached], str(exc)
    slim = [{"id": w["id"], "name": w.get("name")} for w in whs]
    # settings — JSONB; переприсваиваем целиком, иначе SQLAlchemy не заметит мутацию.
    cred.settings = {**(cred.settings or {}), "fbs_warehouses": slim}
    return slim, None


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
    # Заодно обновляем в БД список FBS-складов — чтобы страница привязки читала их из
    # базы, а не дёргала лимитируемый API. Ошибку тут не раздуваем: подключение важнее.
    if ok:
        try:
            whs = await client.list_fbs_warehouses()
            cred.settings = {
                **(cred.settings or {}),
                "fbs_warehouses": [{"id": w["id"], "name": w.get("name")} for w in whs],
            }
        except WBError:
            pass
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
    if ok:
        try:
            whs = await client.list_fbs_warehouses()
            cred.settings = {
                **(cred.settings or {}),
                "fbs_warehouses": [{"id": w["id"], "name": w.get("name")} for w in whs],
            }
        except OzonError:
            pass
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


# --- привязка складов 4tochki к FBS-складам площадок -------------------------


async def _platform_cred(
    session: SessionDep, supplier_id: int, platform: str
) -> Credential | None:
    return (
        await session.execute(
            select(Credential).where(
                Credential.supplier_id == supplier_id, Credential.platform == platform
            )
        )
    ).scalar_one_or_none()


@router.get("/warehouse-mappings", response_model=WarehouseMappingsView)
async def get_warehouse_mappings(
    supplier: SupplierDep, session: SessionDep
) -> WarehouseMappingsView:
    """Данные для блока привязки: выбранные склады 4tochki, FBS-склады площадок (из БД)
    и сохранённые привязки. FBS-склады читаем из cred.settings — их кладёт туда проверка
    подключения. Если кэш пуст, делаем одну живую попытку и сохраняем результат.
    """
    ft_cred = await _platform_cred(session, supplier.id, Platform.FOURTOCHKI)
    selected_ids = set(ft_cred.selected_warehouses) if ft_cred else set()

    # Реальный остаток по каждому складу 4tochki (сумма по всем товарам поставщика) —
    # чтобы в привязке было видно, сколько штук даёт склад и сколько уйдёт на FBS.
    stock_by_wrh: dict[int, int] = {
        wrh: int(total or 0)
        for wrh, total in (
            await session.execute(
                select(ProductStock.wrh, func.sum(ProductStock.rest))
                .join(Product, Product.id == ProductStock.product_id)
                .where(Product.supplier_id == supplier.id)
                .group_by(ProductStock.wrh)
            )
        ).all()
    }

    ft_warehouses = [
        WarehouseOut(
            **{k: w.get(k) for k in ("id", "name", "short_name", "logistic_days")},
            total_rest=stock_by_wrh.get(w["id"], 0),
        )
        for w in (ft_cred.warehouses if ft_cred else [])
        if w["id"] in selected_ids
    ]

    saved: dict[str, list[WarehouseMapping]] = {}
    for m in (
        await session.execute(
            select(WarehouseMapping).where(WarehouseMapping.supplier_id == supplier.id)
        )
    ).scalars().all():
        saved.setdefault(m.platform, []).append(m)

    platforms: list[PlatformMappingView] = []
    cache_updated = False
    for platform in MP_PLATFORMS:
        cred = await _platform_cred(session, supplier.id, platform)
        configured = cred is not None and bool(cred.secrets_encrypted)
        view = PlatformMappingView(
            platform=platform,
            configured=configured,
            available=False,
            mappings=[WarehouseMappingOut.model_validate(m) for m in saved.get(platform, [])],
        )
        if not configured:
            view.message = "Доступы к площадке не заданы"
        else:
            fbs = (cred.settings or {}).get("fbs_warehouses") or []
            error = None
            if not fbs:  # кэш пуст — одна живая попытка (и сохранение)
                fbs, error = await _refresh_fbs_cache(cred)
                cache_updated = cache_updated or error is None
            disabled = set((cred.settings or {}).get("fbs_disabled") or [])
            view.fbs_warehouses = [
                FbsWarehouseOut(id=w["id"], name=w.get("name"), enabled=w["id"] not in disabled)
                for w in fbs
            ]
            view.available = bool(fbs)
            if not fbs:
                view.message = (
                    f"Не удалось получить FBS-склады: {error}"
                    if error
                    else "На площадке нет FBS-складов — создайте склад в кабинете и проверьте подключение"
                )
        platforms.append(view)

    if cache_updated:
        await session.commit()

    return WarehouseMappingsView(fourtochki_warehouses=ft_warehouses, platforms=platforms)


@router.put("/warehouse-mappings/{platform}", response_model=WarehouseMappingsView)
async def set_warehouse_mappings(
    platform: str,
    payload: WarehouseMappingsIn,
    supplier: SupplierDep,
    session: SessionDep,
) -> WarehouseMappingsView:
    """Заменить привязки складов для площадки целиком.

    Проще заменять полностью, чем дифать: набор маленький, а частичные правки на клиенте
    легко рассинхронизировать с сервером.
    """
    if platform not in MP_PLATFORMS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестная площадка")

    # Привязывать можно только выбранные склады 4tochki — иначе заказ уедет со склада,
    # остатки которого мы не публикуем.
    ft_cred = await _platform_cred(session, supplier.id, Platform.FOURTOCHKI)
    selected = set(ft_cred.selected_warehouses) if ft_cred else set()
    bad = [m.fourtochki_wrh for m in payload.mappings if m.fourtochki_wrh not in selected]
    if bad:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Склады 4tochki не в списке выбранных: {sorted(set(bad))}",
        )

    # Один склад 4tochki — максимум к одному FBS-складу (ограничение модели). Ловим
    # дубли заранее, иначе UNIQUE-constraint отдаст невнятную 500.
    seen: set[int] = set()
    dup = [m.fourtochki_wrh for m in payload.mappings if m.fourtochki_wrh in seen or seen.add(m.fourtochki_wrh)]
    if dup:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Склад 4tochki привязан к нескольким FBS-складам: {sorted(set(dup))}",
        )

    # Имена FBS-складов берём из кэша в БД — без лишнего живого запроса к площадке.
    cred = await _platform_cred(session, supplier.id, platform)
    cached = (cred.settings or {}).get("fbs_warehouses") if cred else None
    name_by_id = {w["id"]: w.get("name") for w in (cached or [])}

    # Состояние вкл/выкл FBS-складов сохраняется здесь же, вместе с привязками (по одной
    # кнопке «Сохранить»), а не по каждому переключению тумблера.
    if cred is not None:
        cred.settings = {
            **(cred.settings or {}),
            "fbs_disabled": sorted(set(payload.disabled_fbs)),
        }

    await session.execute(
        delete(WarehouseMapping).where(
            WarehouseMapping.supplier_id == supplier.id,
            WarehouseMapping.platform == platform,
        )
    )
    for m in payload.mappings:
        session.add(
            WarehouseMapping(
                supplier_id=supplier.id,
                platform=platform,
                fourtochki_wrh=m.fourtochki_wrh,
                fbs_warehouse_id=m.fbs_warehouse_id,
                fbs_warehouse_name=name_by_id.get(m.fbs_warehouse_id),
                priority=m.priority,
            )
        )
    await session.commit()

    # Пуш остатков: учтёт новые привязки и вкл/выкл (выключенным FBS зальёт 0). Так
    # пользователю не нужно отдельно жать «отправить остатки».
    await enqueue_kind(session, supplier.id, "push")

    return await get_warehouse_mappings(supplier, session)
