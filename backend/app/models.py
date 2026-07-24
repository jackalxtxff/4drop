from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class Platform(StrEnum):
    FOURTOCHKI = "fourtochki"
    WB = "wb"
    OZON = "ozon"


class ConnectionStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    OK = "ok"
    ERROR = "error"


class IntegrationStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"        # отправлено, ждём модерацию
    ACTIVE = "active"
    REJECTED = "rejected"
    ERROR = "error"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Supplier(Base):
    """Центральная сущность: к поставщику привязаны доступы, каталог, заказы, синхронизации."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    catalog_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    credentials: Mapped[list["Credential"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )


class Credential(Base):
    """Доступы к одной площадке в рамках поставщика. На MVP — один набор на площадку."""

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("supplier_id", "platform", name="uq_credential_platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))

    # Шифротекст (Fernet). В открытом виде секреты в БД не попадают.
    # 4tochki: {"login": ..., "password": ...}
    # WB:      {"content": ..., "prices": ..., "marketplace": ...}
    # Ozon:    {"client_id": ..., "api_key": ...}
    secrets_encrypted: Mapped[str | None] = mapped_column(Text, default=None)

    # Нечувствительные хвосты для отображения в UI, напр. {"api_key": "••••a1b2"}
    secrets_masked: Mapped[dict] = mapped_column(JSONB, default=dict)

    status: Mapped[str] = mapped_column(String(32), default=ConnectionStatus.NOT_CONFIGURED)
    status_message: Mapped[str | None] = mapped_column(Text, default=None)
    # Наименование кабинета/продавца с площадки (для видного бейджа в UI).
    account_name: Mapped[str | None] = mapped_column(String(255), default=None)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Только для 4tochki: справочник складов из GetWarehouses и выбранные пользователем id.
    warehouses: Mapped[list] = mapped_column(JSONB, default=list)
    selected_warehouses: Mapped[list] = mapped_column(JSONB, default=list)

    # Прочие настройки площадки: для WB — id склада FBS продавца (создаётся при
    # первом пуше остатков), контур sandbox/prod и т.п.
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    supplier: Mapped[Supplier] = relationship(back_populates="credentials")


class Product(Base):
    """Товар из каталога 4tochki. Ключ — CAE (code) в рамках поставщика."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("supplier_id", "cae", name="uq_product_cae"),
        Index("ix_products_supplier_brand", "supplier_id", "brand"),
        Index("ix_products_supplier_season", "supplier_id", "season"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )

    cae: Mapped[str] = mapped_column(String(64), index=True)
    goods_type: Mapped[str] = mapped_column(String(32))  # tyre | rim | camera | ...

    brand: Mapped[str | None] = mapped_column(String(255), index=True)
    model: Mapped[str | None] = mapped_column(String(255))
    name: Mapped[str | None] = mapped_column(Text)
    season: Mapped[str | None] = mapped_column(String(32))
    thorn: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # Атрибуты, которые 4tochki показывают в карточке на сайте. Продублированы из
    # attrs в колонки: по JSONB не построить нормальный индекс под фильтр и
    # сортировку на каталоге в десятки тысяч позиций.
    tyre_type: Mapped[str | None] = mapped_column(String(32), index=True)  # attrs.type
    constr: Mapped[str | None] = mapped_column(String(16))                 # R, D, ...
    # Не только «TL»/«TT»: у цельнолитых шин сюда приходит описание целиком,
    # напр. «Цельнолитая немаркая с бортом».
    camera: Mapped[str | None] = mapped_column(String(64))
    noise: Mapped[str | None] = mapped_column(String(16))                  # «71dB»
    strengthening: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # Типоразмер. Для шин width/height/diameter, для дисков — width/diameter + attrs.
    width: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    height: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    diameter: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    load_index: Mapped[str | None] = mapped_column(String(16))
    speed_index: Mapped[str | None] = mapped_column(String(16))

    img_small: Mapped[str | None] = mapped_column(Text)
    img_big: Mapped[str | None] = mapped_column(Text)

    weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    tn_ved: Mapped[int | None] = mapped_column(BigInteger)

    # Полный контейнер из GetGoodsInfo — чтобы не терять атрибуты, которых нет в колонках.
    attrs: Mapped[dict] = mapped_column(JSONB, default=dict)

    # Агрегаты по выбранным складам, пересчитываются синхронизацией.
    # Держим денормализованно ради серверной сортировки и фильтрации по большому каталогу.
    total_rest: Mapped[int] = mapped_column(Integer, default=0, index=True)
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), index=True)
    price_rozn: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    integration_status: Mapped[str] = mapped_column(
        String(32), default=IntegrationStatus.NONE, index=True
    )

    # Ручная блокировка синхронизации. Заблокированный товар не создаётся и не
    # обновляется на маркетплейсах ни авто-, ни вручную; его остаток на площадке
    # форсится в 0 — защита от продажи снятого с продажи. Снимается только вручную.
    sync_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    stocks: Mapped[list["ProductStock"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class ProductStock(Base):
    """Цена и остаток по конкретному складу — как их отдаёт GetGoodsPriceRestByCode.

    Храним по складам, а не агрегатом: при смене набора выбранных складов агрегат
    надо пересчитать, а не выкачивать каталог заново.
    """

    __tablename__ = "product_stocks"
    __table_args__ = (UniqueConstraint("product_id", "wrh", name="uq_stock_product_wrh"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    wrh: Mapped[int] = mapped_column(Integer)          # id склада из GetWarehouses
    rest: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))       # закупочная
    price_rozn: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # рекомендованная розничная
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    product: Mapped[Product] = relationship(back_populates="stocks")


class ProductLink(Base):
    """Маппинг CAE → карточка на площадке. Стержень сценариев 2 и 3."""

    __tablename__ = "product_links"
    __table_args__ = (
        UniqueConstraint("product_id", "platform", name="uq_link_product_platform"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))

    # WB: nm_id + chrt_id. Остатки WB передаются по chrtId, не по sku — маппинг обязан его хранить.
    nm_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    chrt_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    barcode: Mapped[str | None] = mapped_column(String(64), index=True)

    # Ozon
    offer_id: Mapped[str | None] = mapped_column(String(128), index=True)
    ozon_product_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    ozon_sku: Mapped[int | None] = mapped_column(BigInteger, index=True)

    status: Mapped[str] = mapped_column(String(32), default=IntegrationStatus.PENDING)
    status_message: Mapped[str | None] = mapped_column(Text)
    task_id: Mapped[str | None] = mapped_column(String(128))  # id задачи импорта на площадке

    # Хэш атрибутивной части карточки (характеристики, название, картинки — БЕЗ цены
    # и остатка). По нему задача обновления карточек понимает, изменились ли атрибуты
    # в 4tochki, и досылает карточку на площадку только когда есть что менять —
    # иначе каждая правка гоняла бы карточку на модерацию впустую.
    card_hash: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PricingRule(Base):
    """Наценка: закупочная × (1 + percent/100) + fixed, с округлением.

    Правило без brand и goods_type — базовое для поставщика; более специфичное
    переопределяет его.
    """

    __tablename__ = "pricing_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str | None] = mapped_column(String(32))  # None = обе площадки
    goods_type: Mapped[str | None] = mapped_column(String(32))
    brand: Mapped[str | None] = mapped_column(String(255))

    percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=Decimal("0"))
    fixed: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    round_to: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"))
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Order(Base):
    """Заказ с маркетплейса и связанный с ним заказ в 4tochki."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("supplier_id", "platform", "mp_order_id", name="uq_order_mp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))
    mp_order_id: Mapped[str] = mapped_column(String(128), index=True)
    # supplierStatus — статус, которым управляет продавец (new/confirm/complete/cancel).
    mp_status: Mapped[str | None] = mapped_column(String(64))
    # wbStatus — статус самой площадки. Нужен отдельно: отмену покупателем видно только
    # здесь (declined_by_client), при supplierStatus, оставшемся new.
    mp_wb_status: Mapped[str | None] = mapped_column(String(64))

    # Заказ в 4tochki. При FBS со своего склада создаётся через CreateOrder
    # (доставка на наш склад). CreateMarketplaceOrder понадобится, если перейдём на realFBS.
    supplier_order_id: Mapped[int | None] = mapped_column(Integer, index=True)
    supplier_order_number: Mapped[str | None] = mapped_column(String(64))
    supplier_status: Mapped[str | None] = mapped_column(String(64))
    # Отметка отмены у поставщика — она же признак идемпотентности: повторный прогон
    # планировщика не должен снова дёргать 4tochki по уже отменённому заказу.
    supplier_cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Счётчик неудачных попыток отмены. 4tochki отменять умеет не всегда (заказ уже
    # в отгрузке, тестовый заказ в статусе «Я формирую»), а планировщик ходит каждые
    # 10 минут — без ограничения он бесконечно долбил бы API заведомо обречённым вызовом.
    supplier_cancel_attempts: Mapped[int] = mapped_column(Integer, default=0)

    # Куда заказ едет на маркетплейсе: FBS-склад продавца, на который пришло
    # сборочное задание (WB order.warehouseId / Ozon posting.warehouse_id).
    fbs_warehouse_id: Mapped[str | None] = mapped_column(String(64))
    fbs_warehouse_name: Mapped[str | None] = mapped_column(String(255))

    # Откуда берём товар: склад 4tochki, выбранный по привязке WarehouseMapping для
    # этого FBS-склада (с учётом наличия остатка). Проставляется при синхронизации
    # заказов, чтобы было видно «из какого склада точки поедет заказ».
    source_warehouse_id: Mapped[int | None] = mapped_column(Integer)
    source_warehouse_name: Mapped[str | None] = mapped_column(String(255))

    items: Mapped[list] = mapped_column(JSONB, default=list)  # [{cae, name, qty, price, nm_id, chrt_id}]
    error: Mapped[str | None] = mapped_column(Text)

    # Тестовый заказ из песочницы маркетплейса — не настоящая продажа. Показываем
    # бейджем, чтобы тестовые заказы нельзя было спутать с боевыми.
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class WarehouseMapping(Base):
    """Привязка склада 4tochki к FBS-складу маркетплейса.

    FBS: у продавца на площадке есть свои склады, а физически товар лежит на складах
    4tochki. Эта таблица говорит, какой склад 4tochki обслуживает какой FBS-склад —
    по ней при заказе понимаем, из какой точки его собирать.

    Мультисклад: у каждого FBS-склада свой адрес доставки 4tochki (город приёмки).
    От адреса зависят и набор доступных складов, и сроки с каждого — поэтому адрес
    хранится в привязке: по нему оформляется заказ (CreateOrder), чтобы товар приехал
    в нужный город. Пример: ижевские склады → FBS Ижевск, московские → FBS Москва.

    Ключ — (поставщик, площадка, склад 4tochki): каждый склад 4tochki привязывается
    максимум к одному FBS-складу. Это не только про порядок, но и про оверселл: если
    один склад кормил бы два FBS, его остаток опубликовался бы дважды. Несколько
    складов 4tochki могут указывать на один FBS-склад; при заказе среди них выбирается
    тот, где есть остаток (по priority — меньше значит предпочтительнее).
    """

    __tablename__ = "warehouse_mappings"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "platform", "fourtochki_wrh", name="uq_whmap_source"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))  # wb | ozon

    # Склад 4tochki (id из GetWarehouses для адреса address_id).
    fourtochki_wrh: Mapped[int] = mapped_column(Integer)

    # Адрес доставки 4tochki, через который работает этот склад (город приёмки).
    # Один и тот же склад из разных адресов едет разное время, поэтому пара
    # (склад, адрес) — это и есть источник. По нему оформляем CreateOrder.
    address_id: Mapped[int | None] = mapped_column(Integer, default=None)

    # FBS-склад маркетплейса. id у площадок числовой, но храним строкой — у WB и Ozon
    # разные пространства id, строка избавляет от путаницы типов.
    fbs_warehouse_id: Mapped[str] = mapped_column(String(64))
    fbs_warehouse_name: Mapped[str | None] = mapped_column(String(255))

    priority: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class MissingStrategy(StrEnum):
    """Что делать с товаром, который пропал из выдачи 4tochki.

    Поиск 4tochki отдаёт только позиции с остатком, поэтому «пропал» почти всегда
    значит «кончился», а не «снят с продажи». Отсюда безопасный дефолт — обнулить
    остаток, сохранив карточку: товар вернётся на склад, и остаток восстановится
    сам, без перезаливки карточки на маркетплейсе.
    """

    ZERO_STOCK = "zero_stock"
    DELETE = "delete"


class SyncSettings(Base):
    """Расписание фоновых обновлений. Один набор на поставщика.

    Интервал 0 = задача выключена.
    """

    __tablename__ = "sync_settings"
    __table_args__ = (UniqueConstraint("supplier_id", name="uq_sync_settings_supplier"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )

    # Полная выгрузка каталога: тянет карточки и атрибуты. Дорогая, десятки минут.
    catalog_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)

    # Цены и остатки по уже известным CAE. Дёшево, это и есть защита от оверселла.
    stocks_interval_minutes: Mapped[int] = mapped_column(Integer, default=15)

    # Отправка цен и остатков на WB и Ozon.
    push_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)

    # Проверка новых заказов на площадках. Часто: у FBS жёсткий дедлайн сборки, и
    # пропущенный заказ — это срыв срока. Вебхуков по заказам у WB нет, поэтому опрос.
    orders_interval_minutes: Mapped[int] = mapped_column(Integer, default=10)

    # Автоматически оформлять заказ в 4tochki, как только заказ найден и у него
    # определён склад-источник. Выключено по умолчанию: это реальная закупка у
    # поставщика, включать её должен человек осознанно.
    orders_auto_supplier: Mapped[bool] = mapped_column(Boolean, default=False)

    # Обновление атрибутов карточек (характеристики, название, картинки), когда они
    # изменились в 4tochki. Дорого: каждое обновление = повторная модерация, поэтому
    # по умолчанию раз в сутки, и досылается только реально изменившееся.
    cards_update_interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)

    # Полностью автоматический режим: товар, появившийся В НАЛИЧИИ и ещё не заведённый
    # на маркетплейс, система сама создаёт карточкой (не заливая при этом весь каталог
    # с нулями). Пуш цен/остатков продолжает работать по всем активным карточкам.
    auto_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    # Интервал авто-создания карточек. Не слишком часто: каждое создание = модерация.
    auto_cards_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # Предохранитель: сколько карточек создаём за один авто-прогон, чтобы не упереться
    # в rate limit WB и не завалить модерацию тысячами карточек разом.
    auto_cards_batch_limit: Mapped[int] = mapped_column(Integer, default=50)

    missing_strategy: Mapped[str] = mapped_column(
        String(32), default=MissingStrategy.ZERO_STOCK
    )

    # Буфер остатка: сколько штук вычитать из реального остатка перед отправкой на
    # маркетплейс. Реальный остаток 4tochki в products.total_rest НЕ меняем — буфер
    # применяется только к количеству, публикуемому на площадке (see stock.py).
    # Пример: буфер 2, реальный 8 → на МП 6; реальный ≤2 → на МП 0.
    stock_buffer: Mapped[int] = mapped_column(Integer, default=0)

    # Формулы ценообразования — отдельно по площадкам, у WB и Ozon разные комиссии.
    # Переменные: purchase (закупочная), rrp (розница 4tochki), weight. См. formula.py.
    wb_price_formula: Mapped[str] = mapped_column(
        String(500), default="round_to(purchase * 1.25, 10)"
    )
    ozon_price_formula: Mapped[str] = mapped_column(
        String(500), default="round_to(purchase * 1.3, 10)"
    )

    # Цена «до скидки» (зачёркнутая). Считается после основной цены, поэтому в её
    # формуле доступна переменная нашей цены (price / wb_price / ozon_price).
    # WB: из цены до скидки и нашей цены выводится процент скидки. Ozon: обе цены числом.
    wb_price_before_formula: Mapped[str] = mapped_column(
        String(500), default="round_to(wb_price * 1.4, 100)"
    )
    ozon_price_before_formula: Mapped[str] = mapped_column(
        String(500), default="round_to(ozon_price * 1.4, 100)"
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))  # catalog | stocks_prices | cards | orders
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)

    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)

    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[int | None] = mapped_column(ForeignKey("sync_jobs.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    platform: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )
