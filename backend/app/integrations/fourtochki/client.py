"""SOAP-клиент 4tochki B2B.

Контракт снят с http://api-b2b.4tochki.ru/WCF/ClientService.svc?wsdl — см. docs/4tochki-api.md.
Здесь единственное место в системе, которое знает про SOAP; наружу отдаём обычные DTO.

Особенности, продиктованные контрактом:
  * сессии/токена нет — login и password уходят параметрами КАЖДОГО вызова;
  * цена и остаток приходят разбитыми по складам (wh_price_rest);
  * каталог тянется в два шага: поиск → список CAE → GetGoodsInfo пачками.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import zeep
from requests import Session
from zeep.cache import SqliteCache
from zeep.exceptions import Error as ZeepError
from zeep.helpers import serialize_object
from zeep.transports import Transport

from app.config import get_settings

log = logging.getLogger(__name__)

# Контейнеры GetGoodsInfo → наш тип товара.
GOODS_CONTAINERS = {
    "tyreList": "tyre",
    "rimList": "rim",
    "wheelList": "wheel",
    "cameraList": "camera",
    "fastenerList": "fastener",
    "oilList": "oil",
    "pressureSensorList": "pressure_sensor",
    "sparePartList": "spare_part",
}


class FourTochkiError(RuntimeError):
    """Ошибка, пришедшая от 4tochki либо от транспорта."""


@dataclass(slots=True)
class Warehouse:
    id: int
    name: str
    short_name: str | None = None
    key: str | None = None
    logistic_days: int | None = None
    have_delivery: bool = False
    have_pickup: bool = False
    is_paid_delivery: bool = False


@dataclass(slots=True)
class WarehousePriceRest:
    wrh: int
    rest: int
    price: Decimal | None
    price_rozn: Decimal | None


@dataclass(slots=True)
class PriceRest:
    cae: str
    warehouses: list[WarehousePriceRest] = field(default_factory=list)


@dataclass(slots=True)
class GoodsItem:
    cae: str
    goods_type: str
    attrs: dict[str, Any]


@dataclass(slots=True)
class CatalogEntry:
    """Позиция из GetFindTyre/GetFindDisk: карточка + цены/остатки по складам сразу."""

    cae: str
    goods_type: str
    brand: str | None = None
    model: str | None = None
    name: str | None = None
    season: str | None = None
    thorn: bool | None = None
    img_small: str | None = None
    img_big: str | None = None
    warehouses: list[WarehousePriceRest] = field(default_factory=list)


@dataclass(slots=True)
class OrderLine:
    """Строка заказа. Склад указывается на каждую позицию, а не на заказ целиком."""

    cae: str
    qty: int
    warehouse_id: int
    # Если задать priceIn, 4tochki зафиксирует цену (fixPrice). Оставляем None —
    # пусть заказ идёт по актуальной цене поставщика, иначе рискуем поймать отказ
    # при расхождении с их прайсом.
    price_in: Decimal | None = None


@dataclass(slots=True)
class CreatedOrder:
    success: bool
    order_id: int | None
    order_number: str | None
    error: str | None
    item_errors: list[dict[str, Any]] = field(default_factory=list)
    # Позиции, которые 4tochki считает подлежащими обязательной маркировке
    # (Goods.marking_is_required). Для шин ожидаем True — см. вопрос по «Честному знаку».
    marking_required_caes: list[str] = field(default_factory=list)


def _list(container: Any) -> list[Any]:
    """Развернуть WCF-обёртку массива (ArrayOfX) в обычный список.

    zeep отдаёт ArrayOfTyrePriceRest / ArrayOfwh_price_rest / ArrayOfWarehouseInfo
    как объект с единственным полем-списком, а не как список. Итерация по самой
    обёртке молча даёт пустоту — из-за этого выгрузка каталога возвращала 0 позиций.
    Имя внутреннего поля у каждого типа своё, поэтому берём единственный список
    из значений, а не хардкодим имена.
    """
    if container is None:
        return []
    if isinstance(container, list):
        return container
    values = getattr(container, "__values__", None)
    if values:
        for value in values.values():
            if isinstance(value, list):
                return value
    return []


def _err_text(err: Any) -> str | None:
    """Error { code: int, comment: string } — общий для всех ответов."""
    if err is None:
        return None
    code = getattr(err, "code", None)
    comment = getattr(err, "comment", None)
    if not code and not comment:
        return None
    return f"[{code}] {comment}" if code else str(comment)


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class FourTochkiClient:
    """Обёртка над WCF-контрактом.

    zeep синхронный, а вызовы к 4tochki долгие, поэтому каждый метод уводится
    в отдельный поток — иначе он заблокирует event loop FastAPI/arq.
    """

    def __init__(self, login: str, password: str, *, timeout: int = 60) -> None:
        self._login = login
        self._password = password
        settings = get_settings()

        session = Session()
        transport = Transport(
            session=session,
            cache=SqliteCache(path=settings.fourtochki_wsdl_cache, timeout=86400),
            timeout=timeout,
            operation_timeout=timeout,
        )
        self._client = zeep.Client(wsdl=settings.fourtochki_wsdl, transport=transport)
        self._svc = self._client.service

    # --- инфраструктура -------------------------------------------------

    async def _call(self, operation: str, *args: Any) -> Any:
        """Вызвать операцию в пуле потоков, подставив login/password."""

        def run() -> Any:
            op = getattr(self._svc, operation)
            return op(self._login, self._password, *args)

        try:
            return await asyncio.to_thread(run)
        except ZeepError as exc:
            # Ни login, ни password в текст ошибки не попадают.
            raise FourTochkiError(f"4tochki {operation}: {exc}") from exc

    def _factory(self, namespace: str, type_name: str) -> Any:
        return self._client.get_type(f"{{{namespace}}}{type_name}")

    # --- операции -------------------------------------------------------

    async def ping(self) -> bool:
        """Ping(login, password) -> bool. Бэкенд кнопки «Проверить подключение»."""
        return bool(await self._call("Ping"))

    async def get_warehouses(self, address_id: int | None = None) -> list[Warehouse]:
        result = await self._call("GetWarehouses", address_id)

        if getattr(result, "success", None) is False:
            raise FourTochkiError(
                _err_text(getattr(result, "error", None)) or "GetWarehouses: неизвестная ошибка"
            )

        warehouses = _list(getattr(result, "warehouses", None))
        out: list[Warehouse] = []
        for w in warehouses:
            wid = getattr(w, "id", None)
            if wid is None:
                continue
            out.append(
                Warehouse(
                    id=int(wid),
                    name=getattr(w, "name", None) or "",
                    short_name=getattr(w, "shortName", None),
                    key=getattr(w, "key", None),
                    logistic_days=getattr(w, "logisticDays", None),
                    have_delivery=bool(getattr(w, "haveDelivery", False)),
                    have_pickup=bool(getattr(w, "havePickup", False)),
                    is_paid_delivery=bool(getattr(w, "isPaidDelivery", False)),
                )
            )
        return out

    async def get_price_rest(
        self,
        codes: list[str],
        warehouse_ids: list[int] | None = None,
    ) -> list[PriceRest]:
        """GetGoodsPriceRestByCode — цена и остаток по CAE, разбитые по складам.

        price      — закупочная, база для наценки
        price_rozn — рекомендованная розничная от 4tochki
        """
        if not codes:
            return []

        result = await self._call(
            "GetGoodsPriceRestByCode",
            {
                "code_list": {"string": codes},
                "wrh_list": {"int": warehouse_ids} if warehouse_ids else None,
                "searchCodeByOccurence": False,
            },
        )

        if error := _err_text(getattr(result, "error", None)):
            raise FourTochkiError(f"GetGoodsPriceRestByCode: {error}")

        out: list[PriceRest] = []
        for row in _list(getattr(result, "price_rest_list", None)):
            cae = getattr(row, "code", None)
            if not cae:
                continue
            per_wh = [
                WarehousePriceRest(
                    wrh=int(w.wrh),
                    rest=int(getattr(w, "rest", 0) or 0),
                    price=_dec(getattr(w, "price", None)),
                    price_rozn=_dec(getattr(w, "price_rozn", None)),
                )
                for w in _list(getattr(row, "whpr", None))
                if getattr(w, "wrh", None) is not None
            ]
            out.append(PriceRest(cae=cae, warehouses=per_wh))
        return out

    async def get_goods_info(self, codes: list[str]) -> list[GoodsItem]:
        """GetGoodsInfo — атрибуты товаров по списку CAE.

        Ответ разложен по типам (tyreList, rimList, ...); схлопываем в плоский список.
        """
        if not codes:
            return []

        result = await self._call("GetGoodsInfo", {"string": codes})

        if error := _err_text(getattr(result, "error", None)):
            raise FourTochkiError(f"GetGoodsInfo: {error}")

        items: list[GoodsItem] = []
        for container, goods_type in GOODS_CONTAINERS.items():
            for entry in _list(getattr(result, container, None)):
                cae = getattr(entry, "code", None)
                if not cae:
                    continue
                attrs = serialize_object(entry, dict)
                items.append(
                    GoodsItem(
                        cae=cae,
                        goods_type=goods_type,
                        attrs={k: _jsonable(v) for k, v in attrs.items()},
                    )
                )
        return items

    # --- пакетные обёртки ------------------------------------------------

    async def _chunked(self, codes: list[str], batch: int, fetch: Any) -> list[Any]:
        """Разбить коды на батчи и опросить их параллельно.

        Ограничения найдены замером, в WSDL их нет:
          * лимит списка у методов РАЗНЫЙ (см. config), сверх него — ошибка [51];
          * выше ~6 одновременных запросов их сервер не ускоряется.
        """
        chunks = [codes[i : i + batch] for i in range(0, len(codes), batch)]
        sem = asyncio.Semaphore(get_settings().fourtochki_concurrency)

        async def one(chunk: list[str]) -> Any:
            async with sem:
                return await fetch(chunk)

        results = await asyncio.gather(*(one(c) for c in chunks))
        return [item for r in results for item in r]

    async def get_price_rest_all(
        self, codes: list[str], warehouse_ids: list[int] | None = None
    ) -> list[PriceRest]:
        """Цены и остатки по всему списку CAE. Лимит метода — 2000 кодов за запрос.

        warehouse_ids сужает ответ до нужных складов: сам API возвращает whpr по
        всем складам, а фильтр отдаёт только выбранные — заметно меньше данных для
        парсинга и записи в БД, если работаем с несколькими складами из многих.
        """
        if not codes:
            return []
        return await self._chunked(
            codes,
            get_settings().fourtochki_price_batch_size,
            lambda c: self.get_price_rest(c, warehouse_ids=warehouse_ids),
        )

    async def get_rest_codes(self, warehouse_ids: list[int]) -> set[str]:
        """Все CAE с остатком на указанных складах через GetRest — авторитетный
        источник ассортимента.

        GetFindTyre/GetFindDisk (поиск) неполны: не отдают часть товаров, реально
        лежащих на складе (напр. отдельные шипованные модели). GetRest по складу
        возвращает всё, что там есть, — по нему и строим каталог.

        page нумеруется С НУЛЯ; пустая страница — конец.
        """
        rest_filter = self._factory(
            "http://schemas.datacontract.org/2004/07/"
            "TS3.Domain.Models.Client.ClientSoapService.GetRest",
            "getRestFilter",
        )

        async def codes_of(wrh: int) -> set[str]:
            found: set[str] = set()
            page = 0
            while page < 100:  # предохранитель
                result = await self._call("GetRest", rest_filter(wrh=wrh, page=page))
                items = _list(getattr(result, "restItems", None))
                if not items:
                    break
                found.update(i.code for i in items if getattr(i, "code", None))
                page += 1
            return found

        sem = asyncio.Semaphore(get_settings().fourtochki_concurrency)

        async def one(wrh: int) -> set[str]:
            async with sem:
                return await codes_of(wrh)

        results = await asyncio.gather(*(one(w) for w in warehouse_ids))
        return set().union(*results) if results else set()

    async def get_goods_info_all(self, codes: list[str]) -> list[GoodsItem]:
        """Атрибуты по всему списку CAE. Лимит метода — 200 кодов, в 10 раз строже цен."""
        if not codes:
            return []
        return await self._chunked(
            codes,
            get_settings().fourtochki_goods_batch_size,
            self.get_goods_info,
        )

    async def find_tyres(
        self, page: int = 1, page_size: int = 500, **criteria: Any
    ) -> tuple[list[CatalogEntry], int]:
        """GetFindTyre(filter, page, pageSize) -> (позиции, totalPages).

        Отдаёт базовую карточку И цены с остатками по складам (whpr) одним вызовом —
        отдельная проценка через GetGoodsPriceRestByCode для выгрузки каталога не нужна.
        Типоразмеров здесь нет, их добирает GetGoodsInfo.

        criteria — поля FindTyreFilter (brand_list, season_list, diameter_min/max, ...).
        """
        return await self._find(
            "GetFindTyre",
            "tyre",
            "SearchTires",
            "FindTyreFilter",
            page,
            page_size,
            criteria,
        )

    async def find_rims(
        self, page: int = 1, page_size: int = 500, **criteria: Any
    ) -> tuple[list[CatalogEntry], int]:
        return await self._find(
            "GetFindDisk",
            "rim",
            "SearchDiscs",
            "FindDiskFilter",
            page,
            page_size,
            criteria,
        )

    async def _find(
        self,
        operation: str,
        goods_type: str,
        schema: str,
        filter_type: str,
        page: int,
        page_size: int,
        criteria: dict[str, Any],
    ) -> tuple[list[CatalogEntry], int]:
        # Фильтр собираем через фабрику типов, а не голым dict: пустой словарь
        # 4tochki отвергает NullReferenceException'ом, а явный объект со всеми
        # необязательными полями сериализуется так, как ждёт их WCF.
        ns = f"http://schemas.datacontract.org/2004/07/TS3.Domain.Models.Client.ClientSoapService.{schema}"
        filter_obj = self._factory(ns, filter_type)(**criteria)

        result = await self._call(operation, filter_obj, page, page_size)

        if error := _err_text(getattr(result, "error", None)):
            raise FourTochkiError(f"{operation}: {error}")

        total_pages = int(getattr(result, "totalPages", 0) or 0)
        entries: list[CatalogEntry] = []

        for row in _list(getattr(result, "price_rest_list", None)):
            cae = getattr(row, "code", None)
            if not cae:
                continue
            entries.append(
                CatalogEntry(
                    cae=cae,
                    goods_type=goods_type,
                    # У шин бренд лежит в marka, у дисков — в marka же; name уже собран поставщиком.
                    brand=getattr(row, "marka", None),
                    model=getattr(row, "model", None),
                    name=getattr(row, "name", None),
                    season=getattr(row, "season", None),
                    thorn=getattr(row, "thorn", None),
                    img_small=getattr(row, "img_small", None),
                    img_big=getattr(row, "img_big_my", None) or getattr(row, "img_big_pish", None),
                    warehouses=[
                        WarehousePriceRest(
                            wrh=int(w.wrh),
                            rest=int(getattr(w, "rest", 0) or 0),
                            price=_dec(getattr(w, "price", None)),
                            price_rozn=_dec(getattr(w, "price_rozn", None)),
                        )
                        for w in _list(getattr(row, "whpr", None))
                        if getattr(w, "wrh", None) is not None
                    ],
                )
            )

        return entries, total_pages

    async def create_order(
        self,
        items: list[OrderLine],
        *,
        address_id: int | None = None,
        order_number: str | None = None,
        comment: str | None = None,
        is_test: bool = False,
    ) -> CreatedOrder:
        """CreateOrder — обычный B2B-заказ с доставкой на НАШ адрес.

        Это путь для FBS со своего склада: товар едет к нам, отгружаем на МП мы.
        Склад указывается ПОСТРОЧНО (OrderProduct.wrh), а не на заказ целиком —
        позиции с разных складов уезжают в одном заказе.

        order_number пробрасываем в BaseOrder.orderNumber: это наш номер заказа с
        маркетплейса, по нему потом сойдётся сверка. isMarketOrder=True — штатный
        флаг 4tochki для заказов с площадок.

        is_test=True создаёт заказ, не приводящий к реальной отгрузке — использовать
        при отладке, чтобы не заказать шины по-настоящему.
        """
        order = {
            "base_order": {
                "orderNumber": order_number,
                "isMarketOrder": True,
            },
            "comment": comment,
            "address_id": address_id,
            "is_test": is_test,
            "product_list": {
                "OrderProduct": [
                    {
                        "code": line.cae,
                        "quantity": line.qty,
                        "wrh": line.warehouse_id,
                        "priceIn": line.price_in,
                        "fixPrice": line.price_in is not None,
                    }
                    for line in items
                ],
            },
        }
        result = await self._call("CreateOrder", order)
        return self._to_created_order(result)

    async def create_marketplace_order(
        self,
        items: list[tuple[str, int]],
        *,
        contact: dict[str, str],
        delivery: dict[str, Any],
        comment: str | None = None,
    ) -> CreatedOrder:
        """CreateMarketplaceOrder — штатный дропшиппинг-заказ 4tochki.

        Доставка идёт напрямую конечному покупателю. Нужен для realFBS/DBS;
        при FBS со своего склада не используется.
        """
        params = {
            "comment": comment,
            "contact": {
                "name": contact.get("name"),
                "surname": contact.get("surname"),
                "patronymic": contact.get("patronymic"),
                "telephone": contact.get("telephone"),
                "additionalPhone": contact.get("additional_phone"),
            },
            "delivery": {
                "city": delivery.get("city"),
                "street": delivery.get("street"),
                "house": delivery.get("house"),
                "building": delivery.get("building"),
                "building2": delivery.get("building2"),
                "latitude": delivery.get("latitude"),
                "longitude": delivery.get("longitude"),
            },
            "items": {
                "CreateMarketplaceOrderItem": [
                    {"code": cae, "qty": qty} for cae, qty in items
                ],
            },
        }
        result = await self._call("CreateMarketplaceOrder", params)
        return self._to_created_order(result)

    @staticmethod
    def _to_created_order(result: Any) -> CreatedOrder:
        """CreateOrderResult — общий тип у CreateOrder и CreateMarketplaceOrder.

        Построчные ошибки (OrderProductError: code/err/wrh) приходят отдельно от общей
        error: заказ может быть success=False именно из-за одной позиции, и нам нужно
        знать какой — чтобы снять её с продажи, а не гасить весь заказ.
        """
        item_errors = [
            {k: _jsonable(v) for k, v in serialize_object(e, dict).items()}
            for e in _list(getattr(result, "error_product_list", None))
        ]
        marking = [
            g.code
            for g in _list(getattr(result, "goods", None))
            if getattr(g, "marking_is_required", False) and getattr(g, "code", None)
        ]
        return CreatedOrder(
            success=bool(getattr(result, "success", False)),
            order_id=getattr(result, "orderID", None),
            order_number=getattr(result, "orderNumber", None),
            error=_err_text(getattr(result, "error", None)),
            item_errors=item_errors,
            marking_required_caes=marking,
        )

    async def get_order_status(self, order_ids: list[int]) -> list[dict[str, Any]]:
        """GetOrderStatus(filter: FindOrderFilter)."""
        out: list[dict[str, Any]] = []
        for order_id in order_ids:
            result = await self._call("GetOrderStatus", {"orderID": order_id})
            for entry in result or []:
                out.append({k: _jsonable(v) for k, v in serialize_object(entry, dict).items()})
        return out


def _jsonable(value: Any) -> Any:
    """zeep отдаёт Decimal/date/вложенные объекты — приводим к JSON-совместимому виду."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
