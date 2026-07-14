"""Клиент Ozon Seller API.

На этом этапе реализована проверка подключения; создание товаров и синхронизация
остатков/цен добавляются следующим шагом.
"""

from __future__ import annotations

import httpx

BASE = "https://api-seller.ozon.ru"


class OzonError(RuntimeError):
    pass


class OzonClient:
    def __init__(self, client_id: str, api_key: str, *, timeout: float = 30.0) -> None:
        self._headers = {
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    async def check(self) -> tuple[bool, str]:
        """Лёгкий вызов со списком товаров — подтверждает, что пара Client-Id + Api-Key валидна."""
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            try:
                resp = await http.post(
                    f"{BASE}/v3/product/list",
                    headers=self._headers,
                    json={"filter": {}, "limit": 1},
                )
            except httpx.HTTPError as exc:
                return False, f"ошибка сети: {exc.__class__.__name__}"

        if resp.status_code == 200:
            return True, "ок"
        if resp.status_code in (401, 403):
            return False, "неверные Client-Id или Api-Key"
        return False, f"ошибка {resp.status_code}"
