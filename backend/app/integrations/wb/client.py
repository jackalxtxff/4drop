"""Клиент Wildberries Seller API.

На этом этапе реализована проверка подключения; создание карточек и синхронизация
остатков/цен добавляются следующим шагом.

Важно (2026): остатки FBS передаются по chrtId, а не по sku — поэтому ProductLink
хранит chrt_id отдельно от nm_id.
"""

from __future__ import annotations

import httpx

CONTENT_BASE = "https://content-api.wildberries.ru"
PRICES_BASE = "https://discounts-prices-api.wildberries.ru"
MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"

# WB выдаёт ключи по категориям — один ключ может не иметь доступа к другому API.
SCOPES = ("content", "prices", "marketplace")


class WBError(RuntimeError):
    pass


class WBClient:
    def __init__(self, keys: dict[str, str], *, timeout: float = 30.0) -> None:
        # keys: {"content": ..., "prices": ..., "marketplace": ...}
        self._keys = keys
        self._timeout = timeout

    def _headers(self, scope: str) -> dict[str, str]:
        key = self._keys.get(scope)
        if not key:
            raise WBError(f"Не задан API-ключ Wildberries для раздела «{scope}».")
        return {"Authorization": key, "Content-Type": "application/json"}

    async def check(self) -> dict[str, str]:
        """Проверяет каждый заданный ключ отдельно и возвращает статус по разделам.

        Один общий «ок/ошибка» здесь врал бы: ключ content может работать,
        а marketplace — нет, и пользователю нужно видеть, какой именно чинить.
        """
        results: dict[str, str] = {}
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            for scope in SCOPES:
                if not self._keys.get(scope):
                    results[scope] = "не задан"
                    continue
                try:
                    resp = await http.get(
                        f"{CONTENT_BASE}/ping"
                        if scope == "content"
                        else f"{PRICES_BASE}/ping"
                        if scope == "prices"
                        else f"{MARKETPLACE_BASE}/ping",
                        headers=self._headers(scope),
                    )
                    results[scope] = "ок" if resp.status_code == 200 else f"ошибка {resp.status_code}"
                except httpx.HTTPError as exc:
                    results[scope] = f"ошибка сети: {exc.__class__.__name__}"
        return results
