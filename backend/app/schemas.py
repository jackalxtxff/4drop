from datetime import datetime
from decimal import Decimal

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str


# --- поставщики -------------------------------------------------------------


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    comment: str | None = None


class SupplierUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    comment: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    comment: str | None
    is_active: bool
    catalog_synced_at: datetime | None
    product_count: int = 0


# --- подключения ------------------------------------------------------------


class FourTochkiCredentialIn(BaseModel):
    login: str
    password: str
    selected_warehouses: list[int] = []


class WBCredentialIn(BaseModel):
    """У WB токен ОДИН: категории доступа отмечаются при его создании в кабинете."""

    api_key: str


class OzonCredentialIn(BaseModel):
    client_id: str
    api_key: str


class WarehouseOut(BaseModel):
    id: int
    name: str
    short_name: str | None = None
    logistic_days: int | None = None
    have_delivery: bool = False
    is_paid_delivery: bool = False
    # Реальный остаток на складе (сумма по всем товарам). Заполняется в представлении
    # привязки складов, чтобы было видно, сколько штук уходит на FBS-склад.
    total_rest: int | None = None


class AddressOut(BaseModel):
    """Адрес доставки 4tochki. Заводится в ЛК поставщика, мы только читаем."""

    id: int
    title: str
    is_default: bool = False
    # Сколько складов доступно с этого адреса и сколько из них «день в день» —
    # чтобы выбор адреса был осознанным (от него зависят и склады, и сроки).
    warehouse_count: int | None = None
    same_day_count: int | None = None


class AddressSelectIn(BaseModel):
    """Выбор активного адреса доставки 4tochki."""

    address_id: int


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    platform: str
    status: str
    status_message: str | None
    account_name: str | None = None
    checked_at: datetime | None
    secrets_masked: dict
    warehouses: list[WarehouseOut] = []
    selected_warehouses: list[int] = []
    # Только 4tochki: адреса доставки и активный (склады и сроки зависят от него).
    addresses: list[AddressOut] = []
    address_id: int | None = None


# --- товары -----------------------------------------------------------------


class ProductLinkOut(BaseModel):
    """Связь товара с карточкой на одной площадке — для бейджей в таблице."""

    platform: str
    status: str
    status_message: str | None = None
    nm_id: int | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    cae: str
    goods_type: str
    brand: str | None
    model: str | None
    name: str | None
    season: str | None
    thorn: bool | None
    tyre_type: str | None
    constr: str | None
    camera: str | None
    noise: str | None
    strengthening: bool | None
    width: Decimal | None
    height: Decimal | None
    diameter: Decimal | None
    load_index: str | None
    speed_index: str | None
    img_small: str | None
    img_big: str | None
    total_rest: int
    # Остаток за вычетом буфера — столько уйдёт на маркетплейс. Считается на бэке,
    # чтобы витрина и реальный пуш пользовались одной формулой.
    marketplace_rest: int = 0
    min_price: Decimal | None
    price_rozn: Decimal | None
    integration_status: str
    sync_blocked: bool = False
    integrations: list[ProductLinkOut] = []


class ProductDetailOut(ProductOut):
    """Карточка товара целиком — для модального окна по клику на наименование.

    Отдаётся отдельным запросом, а не в списке: attrs — это полный контейнер атрибутов
    из GetGoodsInfo (полсотни полей), и класть его в каждую из 200 строк страницы
    значило бы раздувать ответ ради данных, нужных для одного товара.
    """

    weight: Decimal | None = None
    volume: Decimal | None = None
    tn_ved: int | None = None
    # Все атрибуты 4tochki как есть — включая те, что не выведены колонками в таблицу.
    attrs: dict = {}
    updated_at: datetime | None = None


class ProductStockOut(BaseModel):
    """Остаток по одному складу — для всплывающей подсказки над колонкой «Остаток»."""

    wrh: int
    name: str | None
    rest: int
    price: Decimal | None
    logistic_days: int | None
    selected: bool  # входит ли склад в выбранный набор


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int

    # Считаются по тем же фильтрам, что и total: сводка под отфильтрованной
    # выборкой, а не по всему каталогу.
    in_stock_count: int  # моделей с ненулевым остатком
    total_rest: int      # суммарный остаток в штуках
    stock_buffer: int = 0  # текущий буфер поставщика — для подсказки в таблице


class ProductFacets(BaseModel):
    """Значения для выпадающих фильтров — считаются на сервере по всему каталогу."""

    brands: list[str]
    seasons: list[str]
    goods_types: list[str]
    diameters: list[Decimal]
    widths: list[Decimal]
    heights: list[Decimal]  # профиль
    tyre_types: list[str]
    constrs: list[str]
    cameras: list[str]


class IntegrateRequest(BaseModel):
    """Либо явный список id, либо «выбрать всё по фильтру» — тогда id придут с сервера."""

    product_ids: list[int] | None = None
    select_all_matching: bool = False
    platforms: list[str] = Field(min_length=1)  # ["wb"] | ["ozon"] | ["wb", "ozon"]


class BlockRequest(BaseModel):
    product_ids: list[int] = Field(min_length=1)
    blocked: bool


class UnlinkRequest(BaseModel):
    """Разрыв интеграции: удалить связь с площадкой у заблокированных товаров."""

    product_ids: list[int] = Field(min_length=1)
    platform: Literal["wb", "ozon"]


class SyncSettingsIn(BaseModel):
    """Интервал 0 = задача выключена."""

    catalog_interval_minutes: int = Field(ge=0, le=10080)
    stocks_interval_minutes: int = Field(ge=0, le=10080)
    push_interval_minutes: int = Field(ge=0, le=10080)
    orders_interval_minutes: int = Field(ge=0, le=10080)
    orders_auto_supplier: bool = False
    cards_update_interval_minutes: int = Field(ge=0, le=10080)
    auto_mode: bool
    auto_cards_interval_minutes: int = Field(ge=0, le=10080)
    auto_cards_batch_limit: int = Field(ge=1, le=1000)
    missing_strategy: Literal["zero_stock", "delete"]
    stock_buffer: int = Field(ge=0, le=100000)
    wb_price_formula: str = Field(min_length=1, max_length=500)
    ozon_price_formula: str = Field(min_length=1, max_length=500)
    wb_price_before_formula: str = Field(min_length=1, max_length=500)
    ozon_price_before_formula: str = Field(min_length=1, max_length=500)


class SyncSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    catalog_interval_minutes: int
    stocks_interval_minutes: int
    push_interval_minutes: int
    orders_interval_minutes: int
    orders_auto_supplier: bool
    cards_update_interval_minutes: int
    auto_mode: bool
    auto_cards_interval_minutes: int
    auto_cards_batch_limit: int
    missing_strategy: str
    stock_buffer: int
    wb_price_formula: str
    ozon_price_formula: str
    wb_price_before_formula: str
    ozon_price_before_formula: str
    updated_at: datetime


class FormulaPreviewIn(BaseModel):
    formula: str = Field(min_length=1, max_length=500)
    purchase: Decimal = Decimal("5000")
    rrp: Decimal = Decimal("0")
    weight: Decimal = Decimal("0")
    # Наша цена продажи — для формулы цены до скидки (переменная price/wb_price/ozon_price).
    price: Decimal | None = None


class FormulaPreviewOut(BaseModel):
    ok: bool
    price: Decimal | None = None
    error: str | None = None


class SyncJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    status: str
    total: int
    processed: int
    failed: int
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class SyncJobPage(BaseModel):
    items: list[SyncJobOut]
    total: int
    offset: int
    limit: int


# --- заказы и привязки складов ----------------------------------------------


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform: str
    mp_order_id: str
    mp_status: str | None
    mp_wb_status: str | None = None
    is_test: bool = False

    # Куда на маркетплейсе и откуда со склада 4tochki
    fbs_warehouse_id: str | None
    fbs_warehouse_name: str | None
    source_warehouse_id: int | None
    source_warehouse_name: str | None

    # Заказ в 4tochki (создаётся отдельным действием, в тесте — is_test)
    supplier_order_id: int | None
    supplier_order_number: str | None
    supplier_status: str | None
    supplier_cancelled_at: datetime | None = None

    items: list = []
    error: str | None
    created_at: datetime
    updated_at: datetime


class OrdersSyncPlatform(BaseModel):
    """Итог синхронизации по одной площадке — чтобы не терять причину пустого списка."""

    platform: str
    ok: bool
    fetched: int = 0
    message: str | None = None


class OrdersSyncResult(BaseModel):
    orders: list[OrderOut] = []
    platforms: list[OrdersSyncPlatform] = []


class FbsWarehouseOut(BaseModel):
    """FBS-склад маркетплейса из его API."""

    id: str
    name: str | None = None
    # Выключенный склад: на него публикуется остаток 0 (товар с него не продаётся).
    enabled: bool = True
    # Адрес доставки 4tochki, который кормит этот FBS-склад (город приёмки).
    # Задаёт, какие склады 4tochki доступны как источники и куда поедет заказ.
    address_id: int | None = None


class FbsToggleIn(BaseModel):
    enabled: bool


class WarehouseMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    fourtochki_wrh: int
    fbs_warehouse_id: str
    fbs_warehouse_name: str | None = None
    address_id: int | None = None
    priority: int = 0


class WarehouseMappingItemIn(BaseModel):
    """Одна привязка: склад 4tochki (с его адреса) → FBS-склад площадки."""

    fourtochki_wrh: int
    fbs_warehouse_id: str
    address_id: int | None = None
    priority: int = 0


class WarehouseMappingsIn(BaseModel):
    """Полный набор привязок для площадки — заменяет прежний целиком.

    disabled_fbs — id выключенных FBS-складов (на них публикуется остаток 0). Состояние
    вкл/выкл сохраняется здесь же, по кнопке «Сохранить», а не по каждому переключению.

    fbs_addresses — адрес доставки 4tochki для каждого FBS-склада (мультисклад: ижевский
    FBS кормится с ижевского адреса, московский — с московского). Хранится отдельно от
    привязок, чтобы адрес можно было выбрать до того, как отмечены склады.
    """

    mappings: list[WarehouseMappingItemIn] = []
    disabled_fbs: list[str] = []
    fbs_addresses: dict[str, int] = {}


class PlatformMappingView(BaseModel):
    """Что показать в блоке привязки для одной площадки."""

    platform: str
    configured: bool                      # заданы ли доступы к площадке
    available: bool                       # удалось ли получить FBS-склады из API
    message: str | None = None            # почему недоступно (нет доступов / ошибка API)
    fbs_warehouses: list[FbsWarehouseOut] = []
    mappings: list[WarehouseMappingOut] = []


class WarehouseMappingsView(BaseModel):
    """Данные для мультискладовой привязки.

    Склады отдаются в разрезе адресов: у одного склада в разных городах разные сроки,
    и набор складов тоже отличается. UI показывает для каждого FBS-склада только те
    склады, что доступны с выбранного для него адреса.
    """

    # Адреса доставки 4tochki на выбор (со счётчиками складов).
    addresses: list[AddressOut] = []
    # address_id (строкой) → склады, доступные с этого адреса, с его сроками.
    warehouses_by_address: dict[str, list[WarehouseOut]] = {}
    platforms: list[PlatformMappingView] = []
