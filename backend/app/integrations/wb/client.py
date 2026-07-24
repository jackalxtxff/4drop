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

import asyncio
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

    # Глобальный лимитер WB (429) срабатывает на весь аккаунт продавца, а не на метод,
    # и в песочнице он заметно жёстче боевого. Один 429 не должен ронять всю задачу:
    # ждём и повторяем. Задержка растёт линейно — WB не отдаёт Retry-After.
    RETRY_429 = 4
    RETRY_PAUSE = 15.0

    async def _send(self, http: httpx.AsyncClient, method: str, url: str, **kw) -> httpx.Response:
        """Запрос с повтором на 429. Остальные коды возвращаем как есть — их
        разбирают вызывающие, у каждого свой текст ошибки."""
        for attempt in range(self.RETRY_429):
            resp = await http.request(method, url, headers=self._headers, **kw)
            if resp.status_code != 429:
                return resp
            await asyncio.sleep(self.RETRY_PAUSE * (attempt + 1))
        return resp

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
                    resp = await self._send(
                        http, "POST", f"{self._content}/content/v2/cards/upload", json=chunk
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
                    resp = await self._send(
                        http, "POST", f"{self._content}/content/v2/cards/update", json=chunk
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

    async def list_brands(self, subject_id: int) -> dict[str, str]:
        """Реестр брендов категории: {имя_в_нижнем_регистре: каноничное имя WB}.

        GET /api/content/v1/brands?subjectId=… — весь список одним запросом (у WB нет
        серверного фильтра по имени). Нужен, чтобы подставить бренд в точном написании
        реестра: WB принимает бренд только так (напр. «HANKOOK», «Yokohama», «Kama» —
        регистр у брендов разный, угадывать нельзя), иначе «бренда нет на WB».
        """
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(
                f"{self._content}/api/content/v1/brands",
                headers=self._headers,
                params={"subjectId": subject_id},
            )
        if resp.status_code != 200:
            raise WBError(f"Реестр брендов WB: HTTP {resp.status_code} — {_detail(resp)}")
        brands = resp.json().get("brands") or []
        return {b["name"].strip().lower(): b["name"] for b in brands if b.get("name")}

    async def generate_barcodes(self, count: int) -> list[str]:
        """POST /content/v2/barcodes — сгенерировать `count` штрихкодов (EAN) силами WB.

        WB требует свои валидные штрихкоды: карточку примут с любым vendorCode, но
        остатки по «самодельному» штрихкоду потом не пройдут. Поэтому штрихкоды берём
        отсюда, а не генерируем вручную.
        """
        if count <= 0:
            return []
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await self._send(
                http, "POST", f"{self._content}/content/v2/barcodes", json={"count": count}
            )
        if resp.status_code != 200:
            raise WBError(f"Генерация штрихкодов: HTTP {resp.status_code} — {_detail(resp)}")
        data = resp.json().get("data") or []
        if len(data) < count:
            raise WBError(f"WB вернул {len(data)} штрихкодов из запрошенных {count}")
        return list(data)

    async def upload_photo(
        self, nm_id: int, image: bytes, filename: str = "0.jpg"
    ) -> str | None:
        """POST /content/v3/media/file — прикрепить фото к карточке по nmID (бинарно).

        Грузим именно байтами, а не URL через /media/save: источник (4tochki) отдаёт
        картинку только браузерному User-Agent, и WB по ссылке её не скачает. Возвращает
        None при успехе, иначе текст ошибки.
        """
        # Для multipart Content-Type ставит httpx (с boundary) — свой JSON-заголовок
        # сюда передавать нельзя, поэтому собираем заголовки вручную.
        headers = {
            "Authorization": self._headers["Authorization"],
            "X-Nm-Id": str(nm_id),
            "X-Photo-Number": "1",
        }
        files = {"uploadfile": (filename, image, "image/jpeg")}
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                resp = await http.post(
                    f"{self._content}/content/v3/media/file", headers=headers, files=files
                )
            except httpx.HTTPError as exc:
                return exc.__class__.__name__
        if resp.status_code == 200:
            try:
                if not resp.json().get("error"):
                    return None
            except ValueError:
                return None
        return _detail(resp)

    async def cards_map(self) -> dict[str, dict]:
        """vendorCode → {nm_id, chrt_id, barcode} по всем карточкам кабинета.

        Одним проходом с курсором, а не запросом на товар: для сотен позиций
        поштучный поиск упрётся в лимитер WB.
        """
        out: dict[str, dict] = {}
        cursor: dict = {"limit": 100}

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            while True:
                resp = await self._send(
                    http,
                    "POST",
                    f"{self._content}/content/v2/get/cards/list",
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

    async def list_fbs_warehouses(self) -> list[dict]:
        """Склады FBS продавца: [{"id": str, "name": str, "office_id": int|None}].

        Именно на эти склады WB кладёт сборочные задания (order.warehouseId), и к ним
        пользователь привязывает склады 4tochki. Пусто у свежего продавца — тогда
        сначала нужно создать склад (ensure_warehouse) или в кабинете WB.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.get(f"{self._marketplace}/api/v3/warehouses", headers=self._headers)
        if resp.status_code != 200:
            raise WBError(f"Склады FBS: HTTP {resp.status_code} — {_detail(resp)}")
        data = resp.json()
        return [
            {"id": str(w.get("id")), "name": w.get("name"), "office_id": w.get("officeId")}
            for w in (data if isinstance(data, list) else [])
        ]

    async def fbs_orders(self, *, days: int = 14) -> list[dict]:
        """Сборочные задания FBS в нормализованном виде.

        Тянем новые задания (/orders/new — то, что ждёт сборки) и историю за `days`
        (/orders — чтобы список не был пустым, если новых сейчас нет), склеиваем по id.
        Заказы WB построчные: одно задание = одна позиция. Цена приходит в копейках.

        Возвращает список словарей:
          {mp_order_id, mp_status, created_at, fbs_warehouse_id,
           nm_id, chrt_id, article, barcode, qty, price}
        """
        collected: dict[str, dict] = {}
        any_ok = False          # хоть один запрос ответил 200
        last_detail = ""        # причина последнего сбоя — для понятной ошибки

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            # 1) Новые (ещё не в поставке) сборочные задания — главный источник.
            try:
                new = await http.get(
                    f"{self._marketplace}/api/v3/orders/new", headers=self._headers
                )
                if new.status_code == 200:
                    any_ok = True
                    for o in new.json().get("orders") or []:
                        row = _normalize_wb_order(o, status="new")
                        collected[row["mp_order_id"]] = row
                else:
                    last_detail = f"HTTP {new.status_code} — {_detail(new)}"
            except httpx.HTTPError as exc:
                last_detail = exc.__class__.__name__

            # 2) История за период — вспомогательная, best-effort. dateFrom в unix-секундах,
            # WB отдаёт постранично курсором next. Её 429/ошибку НЕ превращаем в отказ:
            # песочница WB жёстко лимитирует историю, а новые задания важнее.
            date_from = _epoch_days_ago(days)
            next_cursor = 0
            for _ in range(20):  # предохранитель от бесконечного курсора
                resp = await http.get(
                    f"{self._marketplace}/api/v3/orders",
                    headers=self._headers,
                    params={"limit": 1000, "next": next_cursor, "dateFrom": date_from},
                )
                if resp.status_code != 200:
                    last_detail = last_detail or f"HTTP {resp.status_code} — {_detail(resp)}"
                    break
                any_ok = True
                body = resp.json()
                orders = body.get("orders") or []
                for o in orders:
                    row = _normalize_wb_order(o, status=o.get("status") or "confirm")
                    # не затираем более точный статус «new», если задание уже в collected
                    collected.setdefault(row["mp_order_id"], row)
                next_cursor = body.get("next") or 0
                if len(orders) < 1000 or not next_cursor:
                    break
                await asyncio.sleep(self.PAUSE_SECONDS)

        # Раз ни один запрос не прошёл — это настоящая ошибка (нет доступа/лимит), а не
        # «заказов нет». Пусть вызывающий покажет причину, а не молчаливый ноль.
        if not any_ok:
            raise WBError(f"Заказы FBS: {last_detail or 'нет ответа'}")

        return list(collected.values())

    async def order_statuses(self, order_ids: list[int]) -> dict[str, dict]:
        """POST /api/v3/orders/status — статусы заданий по их ID.

        Отмену иначе не увидеть: /orders/new отдаёт только ждущие сборки, а в истории
        у задания остаётся его прежний статус. Отмена живёт в двух полях:
        supplierStatus='cancel' (отменил продавец) и wbStatus из семейства canceled/
        declined_by_client/defect (отменил покупатель или WB).

        Возвращает mp_order_id (строкой) → {supplier_status, wb_status, is_cancellable}.
        """
        out: dict[str, dict] = {}
        if not order_ids:
            return out
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(order_ids), 1000):
                resp = await self._send(
                    http,
                    "POST",
                    f"{self._marketplace}/api/v3/orders/status",
                    json={"orders": order_ids[i : i + 1000]},
                )
                if resp.status_code != 200:
                    raise WBError(f"Статусы заданий: HTTP {resp.status_code} — {_detail(resp)}")
                for o in resp.json().get("orders") or []:
                    out[str(o.get("id"))] = {
                        "supplier_status": o.get("supplierStatus"),
                        "wb_status": o.get("wbStatus"),
                        "is_cancellable": bool(o.get("isCancellable")),
                    }
                await asyncio.sleep(self.PAUSE_SECONDS)
        return out

    async def cancel_order(self, order_id: int) -> None:
        """PATCH /api/v3/orders/{orderId}/cancel — отменить задание продавцом.

        Возможна только до передачи задания WB (см. isCancellable в order_statuses):
        иначе WB отвечает 409, и это не сбой интеграции, а запрет по статусу.
        """
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await self._send(
                http, "PATCH", f"{self._marketplace}/api/v3/orders/{order_id}/cancel"
            )
        if resp.status_code not in (200, 204):
            raise WBError(f"Отмена задания {order_id}: HTTP {resp.status_code} — {_detail(resp)}")

    async def test_decline_order(self, order_id: int) -> None:
        """PATCH /api/v3/test/fbs/orders/{orderId}/decline — эмуляция отмены покупателем.

        Только песочница: метод тестового контура. Доступен в течение часа после
        создания задания и только пока оно не переведено на сборку.
        """
        if not self.sandbox:
            raise WBError("Эмуляция отмены доступна только в песочнице WB")
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await self._send(
                http,
                "PATCH",
                f"{self._marketplace}/api/v3/test/fbs/orders/{order_id}/decline",
            )
        if resp.status_code not in (200, 204):
            raise WBError(
                f"Тестовая отмена {order_id}: HTTP {resp.status_code} — {_detail(resp)}"
            )

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
            resp = await self._send(
                http, "POST", f"{self._prices}/api/v2/upload/task", json={"data": items}
            )
        if resp.status_code == 200 and not resp.json().get("error"):
            return len(items), None
        return 0, _detail(resp)

    async def update_stocks(
        self, warehouse_id: int, stocks: list[dict]
    ) -> tuple[int, str | None]:
        """PUT /api/v3/stocks/{warehouseId}. stocks: [{"chrtId": int, "amount": int}].

        Остатки идут по chrtId (ID размера карточки), а не по штрихкоду: на sku WB
        отвечает 400 IncorrectRequest. Важно: имена полей WB не валидирует — при
        неверном ключе вернётся 204, а остаток молча не обновится.
        """
        if not stocks:
            return 0, None
        # WB режет запрос по 1000 позиций; каталог может быть больше.
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(stocks), 1000):
                chunk = stocks[i : i + 1000]
                resp = await self._send(
                    http,
                    "PUT",
                    f"{self._marketplace}/api/v3/stocks/{warehouse_id}",
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


def _epoch_days_ago(days: int) -> int:
    """Unix-секунды N дней назад. Через loop.time нельзя — нужен именно календарный
    момент, поэтому берём now(). Вынесено, чтобы fbs_orders читался без деталей.
    """
    from datetime import UTC, datetime, timedelta

    return int((datetime.now(UTC) - timedelta(days=days)).timestamp())


def _normalize_wb_order(o: dict, *, status: str) -> dict:
    """Сырое сборочное задание WB → плоский словарь для сохранения в Order.

    Цена у WB в копейках (convertedPrice/price) — переводим в рубли. skus — список
    штрихкодов, берём первый: по нему сопоставим товар, если не сойдётся по nmId/chrtId.
    """
    skus = o.get("skus") or []
    price_kopecks = o.get("convertedPrice") or o.get("price") or 0
    return {
        "mp_order_id": str(o.get("id")),
        "mp_status": status,
        "created_at": o.get("createdAt"),
        "fbs_warehouse_id": str(o["warehouseId"]) if o.get("warehouseId") is not None else None,
        "nm_id": o.get("nmId"),
        "chrt_id": o.get("chrtId"),
        "article": o.get("article"),
        "barcode": skus[0] if skus else None,
        "qty": 1,  # сборочное задание WB — всегда одна позиция
        "price": round(price_kopecks / 100, 2),
    }
