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
    AddressOut,
    AddressSelectIn,
    CredentialOut,
    FbsWarehouseOut,
    FourTochkiCredentialIn,
    OzonCredentialIn,
    PlatformMappingView,
    TestModeIn,
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


def _credential_out(cred: Credential) -> CredentialOut:
    """CredentialOut + адреса 4tochki: они лежат в settings, а не колонками в модели."""
    out = CredentialOut.model_validate(cred)
    if cred.platform == Platform.FOURTOCHKI:
        settings = cred.settings or {}
        out.address_id = settings.get("address_id")
        out.addresses = [AddressOut(**a) for a in (settings.get("addresses") or [])]
        # По умолчанию тестовый контур ВКЛЮЧЁН: боевой заказ — это реальная закупка шин,
        # и включать её можно только осознанным действием.
        out.test_mode = settings.get("test_mode", True)
    return out


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
            out.append(_credential_out(cred))
    return out


@router.put("/fourtochki", response_model=CredentialOut)
async def set_fourtochki(
    payload: FourTochkiCredentialIn, supplier: SupplierDep, session: SessionDep
) -> CredentialOut:
    cred = await _get_or_create(session, supplier.id, Platform.FOURTOCHKI)
    _store_secrets(cred, {"login": payload.login, "password": payload.password})
    cred.selected_warehouses = payload.selected_warehouses
    cred.status = ConnectionStatus.NOT_CONFIGURED
    cred.status_message = "Доступы сохранены, подключение не проверено"
    await session.commit()
    await session.refresh(cred)
    return _credential_out(cred)


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


def _warehouses_payload(warehouses) -> list[dict]:
    return [
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


# Сколько адресов опрашиваем ради счётчиков складов. Каждый адрес — отдельный вызов
# GetWarehouses, а 4tochki блокирует за частые запросы, поэтому ставим потолок.
_ADDRESS_PROBE_LIMIT = 10


@router.post("/fourtochki/check", response_model=CredentialOut)
async def check_fourtochki(supplier: SupplierDep, session: SessionDep) -> CredentialOut:
    """Ping → адреса доставки → склады для активного адреса.

    Адрес доставки — корневая настройка: от него зависят и НАБОР складов, и срок
    доставки с каждого (один склад из разных городов едет разное время). Поэтому
    склады всегда запрашиваются под конкретный адрес, а не «вообще».
    """
    cred = await _require(session, supplier.id, Platform.FOURTOCHKI)
    secrets = load_secrets(cred)
    settings = cred.settings or {}

    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        if not await client.ping():
            raise FourTochkiError("Ping вернул False — логин или пароль не приняты")
        account = await client.get_account_name()
        addresses = await client.get_addresses()

        # Активный адрес: ранее выбранный, если он ещё существует, иначе — по умолчанию.
        known = {a.id for a in addresses}
        active_id = settings.get("address_id")
        if active_id not in known:
            active_id = next((a.id for a in addresses if a.is_default), None)
            if active_id is None and addresses:
                active_id = addresses[0].id

        # Склады по каждому адресу: активный нужен для работы, остальные — ради
        # счётчиков «складов / из них день в день», чтобы выбор адреса был осознанным.
        by_address: dict[int, list] = {}
        for a in addresses[:_ADDRESS_PROBE_LIMIT]:
            by_address[a.id] = await client.get_warehouses(a.id)
        if active_id is not None and active_id not in by_address:
            by_address[active_id] = await client.get_warehouses(active_id)
    except (FourTochkiError, KeyError) as exc:
        cred.status = ConnectionStatus.ERROR
        cred.status_message = str(exc)
        cred.checked_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(cred)
        return _credential_out(cred)

    warehouses = by_address.get(active_id, [])
    # Мультисклад: справочник — объединение складов ВСЕХ адресов (уникально по id),
    # он нужен для подписей и проверок. Сроки же у склада свои в каждом городе,
    # поэтому поадресные списки храним отдельно — из них строится привязка к FBS.
    union: dict[int, dict] = {}
    for aid, whs in by_address.items():
        for row in _warehouses_payload(whs):
            union.setdefault(row["id"], row)
    cred.warehouses = list(union.values())
    cred.settings = {
        **settings,
        "address_id": active_id,
        "warehouses_by_address": {
            str(aid): _warehouses_payload(whs) for aid, whs in by_address.items()
        },
        "addresses": [
            {
                "id": a.id,
                "title": a.title,
                "is_default": a.is_default,
                "warehouse_count": len(by_address[a.id]) if a.id in by_address else None,
                "same_day_count": (
                    sum(1 for w in by_address[a.id] if (w.logistic_days or 0) == 0)
                    if a.id in by_address
                    else None
                ),
            }
            for a in addresses
        ],
    }

    # Если пользователь ещё ничего не выбрал, не выбираем за него: молча включить
    # все склады — значит опубликовать остатки складов с долгой логистикой и сорвать SLA.
    cred.status = ConnectionStatus.OK
    cred.account_name = account
    cred.status_message = (
        f"Подключение работает. Адресов доставки: {len(addresses)}, "
        f"складов суммарно: {len(union)}"
    )
    cred.checked_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(cred)
    return _credential_out(cred)


@router.put("/fourtochki/address", response_model=CredentialOut)
async def set_address(
    payload: AddressSelectIn, supplier: SupplierDep, session: SessionDep
) -> CredentialOut:
    """Сменить активный адрес доставки и перечитать склады под него.

    Смена адреса меняет весь набор складов: часть прежних может не существовать для
    нового адреса. Поэтому здесь же чистим то, что стало недействительным — выбор
    складов и привязки к FBS-складам, — и пересчитываем агрегаты остатков. Иначе
    остались бы «фантомные» остатки со складов, которых с этого адреса нет.
    """
    cred = await _require(session, supplier.id, Platform.FOURTOCHKI)
    settings = cred.settings or {}
    known = {a["id"] for a in (settings.get("addresses") or [])}
    if payload.address_id not in known:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Адрес не найден. Проверьте подключение, чтобы обновить список адресов.",
        )

    secrets = load_secrets(cred)
    try:
        client = FourTochkiClient(secrets["login"], secrets["password"])
        warehouses = await client.get_warehouses(payload.address_id)
    except (FourTochkiError, KeyError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"4tochki: {exc}") from exc

    cred.warehouses = _warehouses_payload(warehouses)
    cred.settings = {**settings, "address_id": payload.address_id}

    # Всё, чего нет у нового адреса, становится недействительным.
    available = {w.id for w in warehouses}
    kept = [w for w in (cred.selected_warehouses or []) if w in available]
    dropped = len(cred.selected_warehouses or []) - len(kept)
    cred.selected_warehouses = kept

    removed_maps = (
        await session.execute(
            delete(WarehouseMapping).where(
                WarehouseMapping.supplier_id == supplier.id,
                WarehouseMapping.fourtochki_wrh.not_in(available) if available else True,
            )
        )
    ).rowcount or 0

    # Агрегаты считаем по оставшимся складам: если не осталось ни одного, остатки
    # честно обнулятся — лучше ноль, чем остаток со склада, куда мы не возим.
    await recompute_aggregates(session, supplier.id, kept)
    await session.commit()
    await session.refresh(cred)

    title = next(
        (a["title"] for a in (settings.get("addresses") or []) if a["id"] == payload.address_id),
        str(payload.address_id),
    )
    notes = [f"Адрес: {title}. Складов доступно: {len(warehouses)}"]
    if dropped:
        notes.append(f"снято складов, недоступных с этого адреса: {dropped}")
    if removed_maps:
        notes.append(f"удалено привязок к FBS: {removed_maps}")
    if not kept:
        notes.append("выберите склады заново — остатки сейчас нулевые")
    cred.status_message = ". ".join(notes)
    await session.commit()
    await session.refresh(cred)
    return _credential_out(cred)


@router.put("/fourtochki/test-mode", response_model=CredentialOut)
async def set_test_mode(
    payload: TestModeIn, supplier: SupplierDep, session: SessionDep
) -> CredentialOut:
    """Переключить контур оформления заказов у поставщика.

    Тестовый контур — CreateOrder(is_test=True): 4tochki принимает заказ, но реальной
    отгрузки не происходит. Выключение переводит оформление в боевой режим, то есть
    заказы становятся настоящей закупкой. На уже оформленные заказы не влияет.
    """
    cred = await _require(session, supplier.id, Platform.FOURTOCHKI)
    cred.settings = {**(cred.settings or {}), "test_mode": payload.test_mode}
    await session.commit()
    await session.refresh(cred)
    return _credential_out(cred)


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
) -> CredentialOut:
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
    return _credential_out(cred)


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
    """Данные для мультискладовой привязки.

    Склады отдаём в разрезе адресов: у одного склада в разных городах разные сроки, и
    сами наборы складов отличаются. UI показывает для каждого FBS-склада только те
    склады, что доступны с выбранного для него адреса. FBS-склады читаем из
    cred.settings — их кладёт туда проверка подключения; пустой кэш освежаем разово.
    """
    ft_cred = await _platform_cred(session, supplier.id, Platform.FOURTOCHKI)
    ft_settings = (ft_cred.settings if ft_cred else None) or {}

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

    addresses = [AddressOut(**a) for a in (ft_settings.get("addresses") or [])]
    warehouses_by_address = {
        aid: [
            WarehouseOut(
                **{
                    k: w.get(k)
                    for k in (
                        "id",
                        "name",
                        "short_name",
                        "logistic_days",
                        # Нужен в UI: склады с доставкой прячем в отдельную группу —
                        # их много и для FBS они обычно непригодны по срокам.
                        "have_delivery",
                        "is_paid_delivery",
                    )
                },
                total_rest=stock_by_wrh.get(w["id"], 0),
            )
            for w in whs
        ]
        for aid, whs in (ft_settings.get("warehouses_by_address") or {}).items()
    }

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
            # Адрес каждого FBS-склада: сохранённый выбор, иначе — уже проставленный
            # в привязках этого склада (мультисклад: у каждого FBS свой город приёмки).
            fbs_addr = dict((cred.settings or {}).get("fbs_addresses") or {})
            for m in saved.get(platform, []):
                if m.address_id and m.fbs_warehouse_id not in fbs_addr:
                    fbs_addr[m.fbs_warehouse_id] = m.address_id
            view.fbs_warehouses = [
                FbsWarehouseOut(
                    id=w["id"],
                    name=w.get("name"),
                    enabled=w["id"] not in disabled,
                    address_id=fbs_addr.get(w["id"]),
                )
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

    return WarehouseMappingsView(
        addresses=addresses,
        warehouses_by_address=warehouses_by_address,
        platforms=platforms,
    )


@router.put("/warehouse-mappings/{platform}", response_model=WarehouseMappingsView)
async def set_warehouse_mappings(
    platform: str,
    payload: WarehouseMappingsIn,
    supplier: SupplierDep,
    session: SessionDep,
) -> WarehouseMappingsView:
    """Заменить привязки складов для площадки целиком (мультисклад).

    Проще заменять полностью, чем дифать: набор маленький, а частичные правки на клиенте
    легко рассинхронизировать с сервером.

    Мультисклад: у каждого FBS-склада свой адрес доставки, и привязывать к нему можно
    только те склады 4tochki, что доступны с ЭТОГО адреса. Набор отслеживаемых складов
    (selected_warehouses у 4tochki) выводится автоматически как объединение всех
    привязок по всем площадкам — отдельно выбирать склады больше не нужно.
    """
    if platform not in MP_PLATFORMS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестная площадка")

    ft_cred = await _platform_cred(session, supplier.id, Platform.FOURTOCHKI)
    ft_settings = (ft_cred.settings if ft_cred else None) or {}
    by_address = ft_settings.get("warehouses_by_address") or {}
    known_addresses = {a["id"] for a in (ft_settings.get("addresses") or [])}

    # Адрес FBS-склада: из явного выбора, иначе из самой привязки.
    fbs_addresses = {k: int(v) for k, v in (payload.fbs_addresses or {}).items()}
    for m in payload.mappings:
        if m.address_id and m.fbs_warehouse_id not in fbs_addresses:
            fbs_addresses[m.fbs_warehouse_id] = m.address_id

    unknown_addr = set(fbs_addresses.values()) - known_addresses
    if unknown_addr:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Неизвестные адреса доставки: {sorted(unknown_addr)}. Проверьте подключение 4tochki.",
        )

    # Склад должен быть доступен с адреса своего FBS-склада — иначе заказ туда не уедет
    # (или уедет неделями): наборы складов и сроки у адресов разные.
    for m in payload.mappings:
        addr = fbs_addresses.get(m.fbs_warehouse_id)
        if addr is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Для FBS-склада {m.fbs_warehouse_id} не выбран адрес доставки 4tochki",
            )
        available = {w["id"] for w in (by_address.get(str(addr)) or [])}
        if m.fourtochki_wrh not in available:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Склад {m.fourtochki_wrh} недоступен с выбранного адреса (FBS {m.fbs_warehouse_id})",
            )

    # Один склад 4tochki — максимум к одному FBS-складу. Это и про порядок, и про
    # оверселл: иначе остаток одного склада опубликовался бы на двух FBS сразу.
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

    # Вкл/выкл и адреса FBS-складов сохраняются здесь же, вместе с привязками (по одной
    # кнопке «Сохранить»), а не по каждому переключению.
    if cred is not None:
        cred.settings = {
            **(cred.settings or {}),
            "fbs_disabled": sorted(set(payload.disabled_fbs)),
            "fbs_addresses": {k: int(v) for k, v in fbs_addresses.items()},
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
                address_id=fbs_addresses.get(m.fbs_warehouse_id),
                priority=m.priority,
            )
        )
    await session.flush()

    # Отслеживаемые склады = объединение привязок по ВСЕМ площадкам. Отдельного выбора
    # складов больше нет: склад нужен ровно тогда, когда он кормит какой-то FBS. По
    # этому же набору идут выгрузка остатков и агрегаты в каталоге.
    used = list(
        (
            await session.execute(
                select(WarehouseMapping.fourtochki_wrh)
                .where(WarehouseMapping.supplier_id == supplier.id)
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if ft_cred is not None:
        ft_cred.selected_warehouses = sorted(used)
        await recompute_aggregates(session, supplier.id, sorted(used))

    await session.commit()

    # Пуш остатков: учтёт новые привязки и вкл/выкл (выключенным FBS зальёт 0). Так
    # пользователю не нужно отдельно жать «отправить остатки».
    await enqueue_kind(session, supplier.id, "push")

    return await get_warehouse_mappings(supplier, session)
