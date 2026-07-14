# 4tochki B2B API — фактический справочник

Источник: `http://api-b2b.4tochki.ru/WCF/ClientService.svc?wsdl` (снят 2026-07-14).
Всё ниже извлечено из WSDL/XSD, не из документации и не по памяти.

## Транспорт и авторизация

- **Протокол: SOAP 1.1/1.2 (WCF, `BasicHttpsBinding`)**, не REST/JSON.
- Endpoint: `https://api-b2b.4tochki.ru/WCF/ClientService.svc`
- targetNamespace: `Wcf.ClientService.Client.WebAPI.TS3`
- **Сессии/токена нет.** `login` и `password` — обычные параметры **каждого** вызова.
  Следствие для нас: пароль B2B-кабинета нужен в расшифрованном виде на каждый запрос,
  поэтому шифрование поля в БД + расшифровка только в памяти воркера — обязательны.
- `Ping(login, password) -> bool` — готовая проверка учётки. Это и есть бэкенд
  кнопки «Проверить подключение» для блока 4tochki.

Клиент на Python: `zeep` (SOAP), на Node: `soap`. Кэшировать WSDL локально —
он 138 КБ + 98 XSD, тянуть его на каждый вызов нельзя.

## Операции, которые нам нужны

Всего в контракте 90+ операций. Релевантные:

### Склады
```
GetWarehouses(login, password, addressId: int)
  -> GetWarehousesResult { success: bool, error: Error, warehouses: WarehouseInfo[] }

WarehouseInfo { id: int, key: string, name: string, shortName: string,
                logisticDays: int, haveDelivery: bool, havePickup: bool,
                isPaidDelivery: bool, stoID: int }
```
`logisticDays` — срок поставки со склада. Прямо влияет на SLA маркетплейса:
склады с большим `logisticDays` лучше не включать в остатки для FBS.

### Каталог (атрибуты товара)
```
GetGoodsInfo(login, password, code_list: string[])
  -> ResultGetGoodsInfo { tyreList, rimList, wheelList, cameraList,
                          fastenerList, oilList, pressureSensorList,
                          sparePartList, error }
```
Возвращает **разные контейнеры по типам товара**. Для шин — `TyreContainer`
(54 поля): `code` (= CAE, ключ), `brand`, `model`, `name`, `season`, `thorn`,
`width`/`height`/`diameter`, `load_index`, `speed_index`, `img_small`/`img_big`,
`tn_ved`, `weight`, `volume`, `omolog`, `run_flat`-признаки и т.д.
Для дисков — `RimContainer` (32 поля): `bolts_count`, `bolts_spacing`, `et`,
`dia`, `color`, `width`, `diameter`...

Этого набора хватает, чтобы собрать карточку WB/Ozon без ручного ввода.
`tn_ved` и `weight`/`volume` понадобятся для логистики и маркировки.

**Важно:** `GetGoodsInfo` принимает список кодов, т.е. это обогащение по CAE,
а не выгрузка каталога. Список кодов берётся из `GetFindTyre` / `GetFindDisk` /
`GetFindWheel` / `GetFindCamera` (поиск с фильтрами) или из прайс-листа.

### Остатки и цены — ядро синхронизации
```
GetGoodsPriceRestByCode(login, password, filter: GoodsPriceRestFilter)
  -> { error: Error, price_rest_list: price_rest[] }

GoodsPriceRestFilter {
  code_list: string[]          // обязательное — CAE-коды
  wrh_list: int[]              // склады (id из GetWarehouses)
  include_paid_delivery: bool
  user_address_id: int
  searchCodeByOccurence: bool
}

price_rest { code: string, whpr: wh_price_rest[] }
wh_price_rest { wrh: int, price: decimal, price_rozn: decimal, rest: int }
```
Это главный метод для сценария 2. Цена и остаток приходят **разбитыми по складам**:
`price` — закупочная (наша база для наценки), `price_rozn` — рекомендованная розничная,
`rest` — остаток на складе `wrh`.

Агрегация по складам (сумма остатков по выбранным складам, минимальная/выбранная цена) —
наша логика, API её не делает.

> Не путать с `GetPrice(priceId)` / `GetRest(wrh)` и `SetPrice` / `SetRest` /
> `SetSupplierPrice` — это ветка для клиентов, которые сами **поставляют** товар
> в 4tochki (свои прайс-листы). Нам она не нужна.

### Заказы — есть отдельный метод под маркетплейсы
```
CreateMarketplaceOrder(login, password, marketplaceOrder: CreateMarketplaceOrderParameters)
  -> CreateOrderResult

CreateMarketplaceOrderParameters {
  comment: string
  contact:  { name, patronymic, surname, telephone, additionalPhone }
  delivery: { city, street, house, building, building2, latitude, longitude }
  items:    { code: string, qty: int }[]
}

CreateOrderResult {
  success: bool, orderID: int, orderNumber: string, createDate: dateTime,
  URL: string, error: Error, error_product_list: OrderProductError[], goods: Goods[]
}
```
**Это ровно наш сценарий 3.** 4tochki предусмотрели дропшиппинг: заказ создаётся
с контактом конечного покупателя и адресом доставки, позиции — по CAE + количество.
`error_product_list` — построчные ошибки (например, товар кончился), обрабатывать
отдельно от общей `error`.

Отслеживание:
```
GetOrderStatus(login, password, filter: FindOrderFilter) -> GetOrderStatusResult[]
FindOrderFilter { orderID, orderNumbers[], statusList[], dStart, dEnd,
                  warehouses[], viewArchive }

GetOrderInfo / GetOrderInfo2 — детали заказа
GetChangeOrder(filter: FindChangeOrderFilter) — изменённые заказы (для polling)
GetStatusList — справочник статусов
```
`GetChangeOrder` — то, что нужно для фонового опроса: тянуть только изменившиеся
заказы, а не весь список.

### Маркировка «Честный знак» — методы в API есть
```
CheckInventoryMarkCode      — проверка кода маркировки
SetMarkCodeList             — передача списка кодов маркировки
CheckVerificationCode / CheckVerificationCodeByQRCode
GetReMarkingDocument / GetReMarkingDocumentList / SetReMarkingDocument
```
Существование `SetMarkCodeList` говорит, что коды ЧЗ ходят через этот же контракт.
Но **кто именно выводит товар из оборота при продаже через маркетплейс — вопрос
договора, а не API.** Остаётся блокирующим.

### Прочее, что может пригодиться
- `GetFindTyre` / `GetFindDisk` / `GetFindWheel` / `GetFindCamera` — поиск по каталогу.
- `GetGoodsByCar` + `GetMarkaAvto` / `GetModelAvto` / `GetYearAvto` / `GetModificationAvto` — подбор по авто.
- `GetDeliveryPeriod` — сроки доставки (для SLA).
- `GetTKTerminalList` / `GetTKTerminalPrice` — терминалы ТК.
- `SetOrderStatus`, `SetOrderComment`, `SetCancelDelivery` — управление заказом.

## Что это меняет в архитектуре

1. **SOAP-клиент, а не REST.** Слой `integrations/fourtochki/` инкапсулирует zeep/soap;
   наружу отдаёт нормальные DTO. Остальной код о SOAP знать не должен.
2. **Нет токена → пароль расшифровывается на каждый вызов.** Ключ шифрования только
   из окружения; в логи ни login, ни password не попадают.
3. **Цена и остаток — per-warehouse.** Модель `products` должна хранить остаток
   в разрезе складов (или агрегат + выбранные склады в `credentials`), иначе
   пересчёт при смене набора складов будет невозможен.
4. **`CreateMarketplaceOrder` снимает риск сценария 3** — не нужно эмулировать
   B2B-заказ, есть штатный маркетплейсный.
5. **Каталог тянется в два шага:** поиск (`GetFindTyre`/`GetFindDisk`) → список CAE →
   `GetGoodsInfo` пачками. Плюс `GetGoodsPriceRestByCode` пачками по тем же CAE.
   Пачки батчить, иначе упрёмся в лимиты (лимиты в WSDL не описаны — уточнить у 4tochki).
