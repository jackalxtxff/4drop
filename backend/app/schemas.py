from datetime import datetime
from decimal import Decimal

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
    content: str | None = None
    prices: str | None = None
    marketplace: str | None = None


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


class CredentialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    platform: str
    status: str
    status_message: str | None
    checked_at: datetime | None
    secrets_masked: dict
    warehouses: list[WarehouseOut] = []
    selected_warehouses: list[int] = []


# --- товары -----------------------------------------------------------------


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
    width: Decimal | None
    height: Decimal | None
    diameter: Decimal | None
    load_index: str | None
    speed_index: str | None
    img_small: str | None
    total_rest: int
    min_price: Decimal | None
    price_rozn: Decimal | None
    integration_status: str


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int


class ProductFacets(BaseModel):
    """Значения для выпадающих фильтров — считаются на сервере по всему каталогу."""

    brands: list[str]
    seasons: list[str]
    goods_types: list[str]
    diameters: list[Decimal]


class IntegrateRequest(BaseModel):
    """Либо явный список id, либо «выбрать всё по фильтру» — тогда id придут с сервера."""

    product_ids: list[int] | None = None
    select_all_matching: bool = False
    platforms: list[str] = Field(min_length=1)  # ["wb"] | ["ozon"] | ["wb", "ozon"]


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
