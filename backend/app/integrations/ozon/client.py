"""Клиент Ozon Seller API.

При создании Api-Key в кабинете Ozon выбираются роли («типы токена»). Для этой
интеграции нужны:

  * Product (59 методов)            — создание и обновление товаров, цены, остатки;
  * Description Category (11)       — дерево категорий и характеристики: без него
                                      карточку не собрать, атрибуты обязательны;
  * Warehouse (42)                  — список складов, остатки FBS привязаны к складу;
  * Posting FBS (96)                — заказы FBS: получение и отгрузка.

Желательно добавить:
  * Notification (8)                — push о новых заказах вместо опроса;
  * Certification (9)               — шины подлежат обязательной сертификации;
  * Brand (2)                       — проверка, что бренд разрешён к продаже.

Роль Admin (460 методов) выдаёт доступ ко всему Seller API — не используйте её
ради удобства: токен с такими правами утекает вместе со всей учётной записью.
"""

from __future__ import annotations

import httpx

BASE = "https://api-seller.ozon.ru"


class OzonError(RuntimeError):
    pass


def _message(resp: httpx.Response) -> str:
    """Ozon кладёт причину в тело: {"code": 3, "message": "Invalid Api-Key ..."}."""
    try:
        return str(resp.json().get("message") or "").strip() or f"HTTP {resp.status_code}"
    except ValueError:
        return f"HTTP {resp.status_code}"


def _is_bad_credentials(status_code: int, message: str) -> bool:
    """Отличить «ключ неверный» от «роль не выдана».

    Ozon отвечает 400 «Invalid Api-Key» на неверный ключ и 401 «Client-Id and Api-Key
    headers are required» на пустой — то есть по статусу их не различить, и без разбора
    тела мы бы отправили пользователя включать роли, хотя проблема в самом ключе.
    """
    if status_code not in (400, 401, 403):
        return False
    low = message.lower()
    return (
        "invalid api-key" in low
        or "headers are required" in low
        or "client-id" in low and "invalid" in low
    )


class OzonClient:
    def __init__(self, client_id: str, api_key: str, *, timeout: float = 30.0) -> None:
        self._headers = {
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def check(self) -> tuple[bool, str]:
        """Проверяет пару Client-Id + Api-Key и наличие каждой нужной роли.

        По одному лёгкому методу на роль: 403 на конкретном методе означает, что
        именно эта роль не отмечена в токене. Общее «ок/ошибка» здесь врало бы —
        токен может уметь товары, но не уметь заказы.
        """
        probes: list[tuple[str, str, dict]] = [
            ("Product", "/v3/product/list", {"filter": {}, "limit": 1}),
            ("Description Category", "/v1/description-category/tree", {"language": "RU"}),
            ("Warehouse", "/v1/warehouse/list", {}),
            (
                "Posting FBS",
                "/v3/posting/fbs/list",
                {
                    "dir": "ASC",
                    "filter": {
                        "since": "2024-01-01T00:00:00.000Z",
                        "to": "2024-01-02T00:00:00.000Z",
                    },
                    "limit": 1,
                },
            ),
        ]

        results: list[str] = []
        missing: list[str] = []

        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for role, path, payload in probes:
                try:
                    resp = await http.post(f"{BASE}{path}", headers=self._headers, json=payload)
                except httpx.HTTPError as exc:
                    results.append(f"{role}: сеть недоступна ({exc.__class__.__name__})")
                    missing.append(role)
                    continue

                if resp.status_code == 200:
                    results.append(f"{role}: ок")
                    continue

                # Ozon на неверный ключ отвечает 400 с телом «Invalid Api-Key»,
                # а не 401 — по одному коду статуса «неверный ключ» и «нет роли»
                # не различить, приходится читать тело.
                detail = _message(resp)

                if _is_bad_credentials(resp.status_code, detail):
                    # Ключ не тот — проверять остальные роли бессмысленно.
                    return False, f"Неверные Client-Id или Api-Key ({detail})."

                if resp.status_code == 403:
                    results.append(f"{role}: роль не выдана токену")
                else:
                    results.append(f"{role}: ошибка {resp.status_code} — {detail}")
                missing.append(role)

        ok = not missing
        message = "; ".join(results)
        if missing:
            message += (
                f". Пересоздайте Api-Key в кабинете Ozon, отметив типы токена: "
                f"{', '.join(missing)}."
            )
        return ok, message

    # --- цены и остатки -------------------------------------------------

    async def update_prices(self, items: list[dict]) -> tuple[int, str | None]:
        """POST /v1/product/import/prices.

        В отличие от WB, Ozon принимает обе цены числом напрямую:
          price     — цена продажи (со скидкой),
          old_price — цена до скидки (зачёркнутая); "0" отключает зачёркивание.
        items: [{"offer_id": str, "price": int, "old_price": int}].
        """
        if not items:
            return 0, None
        prices = [
            {
                "offer_id": it["offer_id"],
                "price": str(it["price"]),
                "old_price": str(it.get("old_price") or 0),
                # Автопринятие цены Ozon оставляем включённым по умолчанию.
                "auto_action_enabled": "UNKNOWN",
            }
            for it in items
        ]
        sent = 0
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(prices), 1000):  # Ozon: до 1000 за запрос
                resp = await http.post(
                    f"{BASE}/v1/product/import/prices",
                    headers=self._headers,
                    json={"prices": prices[i : i + 1000]},
                )
                if resp.status_code != 200:
                    return sent, _message(resp)
                sent += len(prices[i : i + 1000])
        return sent, None

    async def update_stocks(self, items: list[dict]) -> tuple[int, str | None]:
        """POST /v2/products/stocks. items: [{"offer_id": str, "stock": int, "warehouse_id": int}]."""
        if not items:
            return 0, None
        stocks = [
            {"offer_id": it["offer_id"], "stock": it["stock"], "warehouse_id": it["warehouse_id"]}
            for it in items
        ]
        sent = 0
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for i in range(0, len(stocks), 100):  # Ozon: до 100 за запрос
                resp = await http.post(
                    f"{BASE}/v2/products/stocks",
                    headers=self._headers,
                    json={"stocks": stocks[i : i + 100]},
                )
                if resp.status_code != 200:
                    return sent, _message(resp)
                sent += len(stocks[i : i + 100])
        return sent, None
