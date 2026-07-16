"""Клиент Wildberries Seller API.

Токен у WB ОДИН, но при его создании в кабинете отмечаются категории доступа.
Поэтому храним один ключ, а проверяем его отдельно по каждому нужному API: ключ
без категории «Контент» даст 401 именно на content-api, и пользователю надо
показать, какую галочку он забыл, а не общее «ошибка».

Для этой интеграции обязательны три категории (и уровень «Чтение и запись»):
  * Контент          — создание и редактирование карточек товара;
  * Цены и скидки    — установка цен с наценкой;
  * Маркетплейс      — остатки FBS и сборочные задания (= заказы).

Остальные категории (Статистика, Финансы, Аналитика, Продвижение, Вопросы и отзывы,
Чат, Поставки, Возвраты, Документы, Пользователи) интеграции не нужны — не включайте,
чтобы не расширять права токена без необходимости.

Важно (2026): остатки FBS передаются по chrtId, а не по sku — см. ProductLink.chrt_id.
"""

from __future__ import annotations

import base64
import json

import httpx

# У каждой категории свой хост, и /ping на нём проверяет доступ именно к ней.
# У песочницы хосты другие: тестовый токен на боевом хосте получает
# «401 token scope not allowed», что легко принять за невыключенную категорию.
PROD_HOSTS: dict[str, tuple[str, str]] = {
    "content": ("Контент", "https://content-api.wildberries.ru"),
    "prices": ("Цены и скидки", "https://discounts-prices-api.wildberries.ru"),
    "marketplace": ("Маркетплейс", "https://marketplace-api.wildberries.ru"),
}

SANDBOX_HOSTS: dict[str, tuple[str, str]] = {
    "content": ("Контент", "https://content-api-sandbox.wildberries.ru"),
    "prices": ("Цены и скидки", "https://discounts-prices-api-sandbox.wildberries.ru"),
    "marketplace": ("Маркетплейс", "https://marketplace-api-sandbox.wildberries.ru"),
}


class WBError(RuntimeError):
    pass


def is_test_token(api_key: str) -> bool:
    """Токен WB — это JWT, и в его payload есть флаг `t`: true у токена песочницы.

    Payload читаем без проверки подписи: она нам не нужна, ключ всё равно проверит
    сам WB. Нужен только контур, иначе будем стучаться не туда.
    """
    try:
        payload_b64 = api_key.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
        return bool(payload.get("t"))
    except (IndexError, ValueError, TypeError):
        # Не JWT или битый — считаем боевым, WB сам ответит понятной ошибкой.
        return False


class WBClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        if not api_key:
            raise WBError("API-ключ Wildberries не задан.")
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}
        self._timeout = timeout
        self.sandbox = is_test_token(api_key)
        self._hosts = SANDBOX_HOSTS if self.sandbox else PROD_HOSTS

    async def check(self) -> tuple[bool, str]:
        """Проверяет ключ по каждой нужной категории отдельно.

        Возвращает (всё ли в порядке, человекочитаемый разбор по категориям).
        Общий «ок/ошибка» здесь врал бы: ключ может иметь «Контент», но не иметь
        «Цены и скидки», и чинить надо конкретную галочку.
        """
        results: list[str] = []
        missing: list[str] = []

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for _scope, (label, base) in self._hosts.items():
                try:
                    resp = await http.get(f"{base}/ping", headers=self._headers)
                except httpx.HTTPError as exc:
                    results.append(f"{label}: сеть недоступна ({exc.__class__.__name__})")
                    missing.append(label)
                    continue

                if resp.status_code == 200:
                    results.append(f"{label}: ок")
                elif resp.status_code in (401, 403):
                    results.append(f"{label}: нет доступа — категория не включена в токене")
                    missing.append(label)
                else:
                    results.append(f"{label}: ошибка {resp.status_code}")
                    missing.append(label)

        env = "песочница" if self.sandbox else "боевой контур"
        message = f"[{env}] " + "; ".join(results)

        if missing:
            message += (
                f". Пересоздайте токен в кабинете WB, отметив категории: {', '.join(missing)}, "
                "и уровень доступа «Чтение и запись»."
            )
        elif self.sandbox:
            message += (
                ". Это тестовый токен: карточки и заказы будут создаваться в песочнице WB, "
                "а не в реальном кабинете."
            )

        return not missing, message

    async def seller_name(self) -> str | None:
        """Наименование продавца из Common API (/api/v1/seller-info).

        Работает только на боевом контуре: у песочницы этого хоста нет, а тестовый
        токен к боевому не имеет доступа — тогда просто возвращаем None (имя не покажем).
        Возвращает торговую марку, иначе название.
        """
        if self.sandbox:
            return None
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                resp = await http.get(
                    "https://common-api.wildberries.ru/api/v1/seller-info",
                    headers=self._headers,
                )
            except httpx.HTTPError:
                return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("tradeMark") or data.get("name")

    # --- карточки -------------------------------------------------------

    # WB режет запросы глобальным лимитером (429), поэтому карточки грузим
    # небольшими пачками с паузой. Значения подобраны под лимиты Content API.
    UPLOAD_CHUNK = 20
    PAUSE_SECONDS = 1.5

    @property
    def _content(self) -> str:
        return self._hosts["content"][1]

    async def upload_cards(self, cards: list[dict]) -> tuple[int, list[str]]:
        """POST /content/v2/cards/upload. Возвращает (сколько отправлено, ошибки).

        Приём карточки (200, error=false) не означает, что она прошла модерацию —
        это только постановка в очередь. Реальный результат появляется позже
        в списке карточек и в /content/v2/cards/error/list.
        """
        sent = 0
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(cards), self.UPLOAD_CHUNK):
                chunk = cards[i : i + self.UPLOAD_CHUNK]
                try:
                    resp = await http.post(
                        f"{self._content}/content/v2/cards/upload",
                        headers=self._headers,
                        json=chunk,
                    )
                except httpx.HTTPError as exc:
                    errors.append(f"сеть: {exc.__class__.__name__}")
                    continue

                if resp.status_code == 200 and not resp.json().get("error"):
                    sent += len(chunk)
                else:
                    detail = _detail(resp)
                    errors.append(f"HTTP {resp.status_code}: {detail}")

                if i + self.UPLOAD_CHUNK < len(cards):
                    await asyncio.sleep(self.PAUSE_SECONDS)

        return sent, errors

    async def update_cards(self, variants: list[dict]) -> tuple[int, list[str]]:
        """POST /content/v2/cards/update — обновление СУЩЕСТВУЮЩИХ карточек.

        Отдельно от upload_cards: upload создаёт новые и падает с «vendor code
        используется в других карточках», если артикул уже занят. update правит
        существующую и требует nmID в каждом варианте.
        """
        updated = 0
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(variants), self.UPLOAD_CHUNK):
                chunk = variants[i : i + self.UPLOAD_CHUNK]
                try:
                    resp = await http.post(
                        f"{self._content}/content/v2/cards/update",
                        headers=self._headers,
                        json=chunk,
                    )
                except httpx.HTTPError as exc:
                    errors.append(f"сеть: {exc.__class__.__name__}")
                    continue

                if resp.status_code == 200 and not resp.json().get("error"):
                    updated += len(chunk)
                else:
                    errors.append(f"HTTP {resp.status_code}: {_detail(resp)}")

                if i + self.UPLOAD_CHUNK < len(variants):
                    await asyncio.sleep(self.PAUSE_SECONDS)

        return updated, errors

    async def cards_map(self) -> dict[str, dict]:
        """vendorCode → {nm_id, chrt_id, barcode} по всем карточкам кабинета.

        Одним проходом с курсором, а не запросом на товар: для сотен позиций
        поштучный поиск упрётся в лимитер WB.
        """
        out: dict[str, dict] = {}
        cursor: dict = {"limit": 100}

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            while True:
                resp = await http.post(
                    f"{self._content}/content/v2/get/cards/list",
                    headers=self._headers,
                    json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
                )
                if resp.status_code != 200:
                    raise WBError(f"Список карточек: HTTP {resp.status_code} — {_detail(resp)}")

                body = resp.json()
                cards = body.get("cards") or []
                for card in cards:
                    sizes = card.get("sizes") or [{}]
                    skus = sizes[0].get("skus") or []
                    out[card.get("vendorCode")] = {
                        "nm_id": card.get("nmID"),
                        "chrt_id": sizes[0].get("chrtID"),
                        "barcode": skus[0] if skus else None,
                    }

                got = body.get("cursor") or {}
                # Курсор WB: пока вернулось столько же, сколько просили, есть ещё страница.
                if len(cards) < cursor["limit"]:
                    break
                cursor = {
                    "limit": 100,
                    "updatedAt": got.get("updatedAt"),
                    "nmID": got.get("nmID"),
                }
                await asyncio.sleep(self.PAUSE_SECONDS)

        return out

    # --- цены и остатки -------------------------------------------------

    @property
    def _prices(self) -> str:
        return self._hosts["prices"][1]

    @property
    def _marketplace(self) -> str:
        return self._hosts["marketplace"][1]

    async def ensure_warehouse(self, name: str = "4drop FBS") -> int:
        """Вернуть id FBS-склада продавца, создав его, если ни одного нет.

        Остатки WB принимает по складу продавца, а не по nmID. Свежий продавец
        складов не имеет, поэтому один заводим сами и запоминаем — второй раз не создаём.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(f"{self._marketplace}/api/v3/warehouses", headers=self._headers)
            if resp.status_code == 200:
                whs = resp.json()
                if isinstance(whs, list) and whs:
                    return whs[0]["id"]

            # Складов нет — создаём. officeId берём из справочника пунктов приёмки.
            offices = await http.get(f"{self._marketplace}/api/v3/offices", headers=self._headers)
            office_id = 1
            if offices.status_code == 200 and offices.json():
                office_id = offices.json()[0]["id"]

            created = await http.post(
                f"{self._marketplace}/api/v3/warehouses",
                headers=self._headers,
                json={"name": name, "officeId": office_id},
            )
            if created.status_code != 200:
                raise WBError(f"Не удалось создать склад FBS: {_detail(created)}")
            return created.json()["id"]

    async def current_prices(self) -> dict[int, tuple[int, int]]:
        """nmID → (цена до скидки, процент скидки). Нужно, чтобы не слать неизменённое:
        WB отклоняет ВЕСЬ батч, если хоть одна пара цена+скидка уже установлена."""
        out: dict[int, tuple[int, int]] = {}
        offset = 0
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            while True:
                resp = await http.get(
                    f"{self._prices}/api/v2/list/goods/filter",
                    headers=self._headers,
                    params={"limit": 1000, "offset": offset},
                )
                if resp.status_code != 200:
                    break
                goods = resp.json().get("data", {}).get("listGoods") or []
                for g in goods:
                    sizes = g.get("sizes") or [{}]
                    # (цена до скидки, процент скидки) — обе нужны, чтобы понять,
                    # изменилось ли что-то, и не гонять неизменённый батч.
                    out[g["nmID"]] = (sizes[0].get("price"), g.get("discount", 0))
                if len(goods) < 1000:
                    break
                offset += 1000
                await asyncio.sleep(self.PAUSE_SECONDS)
        return out

    @staticmethod
    def discount_percent(price_before: int, our_price: int) -> int:
        """Процент скидки, чтобы цена до скидки со скидкой дала нашу цену.

        WB не принимает цену со скидкой напрямую — только цену до скидки и процент.
        Итог получается price_before × (1 − pct/100), поэтому есть погрешность
        целого процента: WB покажет цену, близкую к нашей, но не всегда ровно её.
        """
        if price_before <= 0 or our_price >= price_before:
            return 0
        pct = round((1 - our_price / price_before) * 100)
        return max(0, min(pct, 99))

    async def update_prices(self, items: list[dict]) -> tuple[int, str | None]:
        """POST /api/v2/upload/task. items: [{"nmID": int, "price": int, "discount": int}].

        Цена целочисленная, в рублях. Возвращает (сколько отправлено, ошибка или None).
        """
        if not items:
            return 0, None
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                f"{self._prices}/api/v2/upload/task",
                headers=self._headers,
                json={"data": items},
            )
        if resp.status_code == 200 and not resp.json().get("error"):
            return len(items), None
        return 0, _detail(resp)

    async def update_stocks(
        self, warehouse_id: int, stocks: list[dict]
    ) -> tuple[int, str | None]:
        """PUT /api/v3/stocks/{warehouseId}. stocks: [{"sku": barcode, "amount": int}].

        WB принимает остатки FBS по складу и штрихкоду, а не по nmID/chrtID.
        WB требует валидный штрихкод (EAN): карточка примет любой vendorCode, а
        stocks — нет, поэтому баркоды карточек должны быть настоящими.
        """
        if not stocks:
            return 0, None
        # WB режет запрос по 1000 позиций; каталог может быть больше.
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(stocks), 1000):
                chunk = stocks[i : i + 1000]
                resp = await http.put(
                    f"{self._marketplace}/api/v3/stocks/{warehouse_id}",
                    headers=self._headers,
                    json={"stocks": chunk},
                )
                if resp.status_code not in (200, 204):
                    return i, _detail(resp)
                await asyncio.sleep(self.PAUSE_SECONDS)
        return len(stocks), None

    async def card_errors(self) -> dict[str, str]:
        """vendorCode → текст ошибки. WB отдаёт сюда карточки, не прошедшие проверку."""
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(
                f"{self._content}/content/v2/cards/error/list",
                headers=self._headers,
                params={"locale": "ru"},
            )
            if resp.status_code != 200:
                # 429 здесь не повод валить всю задачу — карточки уже отправлены.
                return {}
            return {
                e.get("vendorCode"): "; ".join(e.get("errors") or [])
                for e in (resp.json().get("data") or [])
                if e.get("vendorCode")
            }


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return str(
            body.get("errorText") or body.get("detail") or body.get("title") or body
        )[:200]
    except ValueError:
        return resp.text[:200]
