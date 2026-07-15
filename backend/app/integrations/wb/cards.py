"""Сборка карточки Wildberries из товара 4tochki.

Все id ниже сняты с живого Content API (песочница), а не взяты из документации:
  * предмет 5283 «Шины автомобильные», 5284 «Диски колесные» (родитель «Шины и диски»);
  * характеристики — из GET /content/v2/object/charcs/5283.

Формально у предмета 5283 нет обязательных характеристик, но карточка без
типоразмера и сезонности бесполезна в поиске WB, поэтому заполняем всё, что есть
в данных поставщика.
"""

from __future__ import annotations

from decimal import Decimal

from app.models import Product

# Предмет WB по типу товара 4tochki.
SUBJECT_BY_TYPE: dict[str, int] = {
    "tyre": 5283,  # Шины автомобильные
    "rim": 5284,   # Диски колесные
}

# id характеристик предмета «Шины автомобильные».
CHAR_WIDTH = 244928        # Ширина, мм
CHAR_HEIGHT = 244948       # Высота профиля, %
CHAR_DIAMETER = 244965     # Диаметр, дюймы
CHAR_SEASON = 244922       # Сезонность
CHAR_RUNFLAT = 244925      # Технология RunFlat
CHAR_THORN = 15001206      # Шипы
CHAR_SPEED = 15001207      # Индекс скорости
CHAR_LOAD = 15001208       # Индекс нагрузки (число)
CHAR_PURPOSE = 15001209    # Назначение шин
CHAR_NOISE = 15002678      # Шумность шины (число, дБ)
CHAR_BRAND = 14177446      # Бренд
CHAR_VENDOR_CODE = 5522881  # Артикул производителя

SEASON_LABEL = {"s": "Летняя", "w": "Зимняя", "u": "Всесезонная"}

# Тип шины 4tochki → «Назначение шин» у WB.
PURPOSE_LABEL = {
    "car": "Легковые",
    "cartruck": "Легкогрузовые",
    "vned": "Внедорожные",
    "truck": "Грузовые",
    "moto": "Мото",
    "quadbike": "Квадроциклы",
}


class CardBuildError(ValueError):
    """Товар нельзя превратить в карточку — причина в тексте."""


# Префикс наших карточек. Нужен, чтобы отличать созданное системой от товаров,
# которые продавец завёл сам: сопоставление идёт по vendorCode, и без метки чужая
# карточка со случайно совпавшим артикулом была бы «присвоена» и ей поменяли бы
# цену/остаток. С префиксом пересечение с чужими артикулами исключено.
VENDOR_PREFIX = "4D-"


def vendor_code(product: Product) -> str:
    """Артикул продавца (vendorCode) для карточки WB. Ключ сопоставления с нашей базой."""
    return f"{VENDOR_PREFIX}{product.cae}"


def is_ours(vendor_code_value: str | None) -> bool:
    """Наша ли это карточка — по префиксу vendorCode. Защита от правки чужих товаров."""
    return bool(vendor_code_value) and vendor_code_value.startswith(VENDOR_PREFIX)


def barcode_for(product: Product) -> str:
    """Штрихкод детерминированный: повторный запуск не наплодит дублей карточек."""
    return f"4D{product.cae}"


def _package_dims(product: Product) -> dict:
    """Габариты упаковки в см.

    WB требует габариты, а 4tochki их не отдаёт — считаем наружный диаметр шины
    из типоразмера: D = посадочный(дюймы)×25.4 + 2×ширина×профиль/100.
    Для дисков и всего, где типоразмера нет, берём осторожные значения по умолчанию.
    """
    width = float(product.width or 0)
    height = float(product.height or 0)
    diameter = float(product.diameter or 0)

    if width and height and diameter:
        outer_mm = diameter * 25.4 + 2 * width * height / 100
        side = max(round(outer_mm / 10), 30)
        return {
            "length": side,
            "width": max(round(width / 10), 10),
            "height": side,
            "weightBrutto": float(product.weight or 10),
        }

    return {
        "length": 70,
        "width": 25,
        "height": 70,
        "weightBrutto": float(product.weight or 10),
    }


def _characteristics(product: Product) -> list[dict]:
    a = product.attrs or {}
    chars: list[dict] = []

    def add(char_id: int, value) -> None:
        if value in (None, "", []):
            return
        chars.append({"id": char_id, "value": value if isinstance(value, list) else [value]})

    if product.width:
        add(CHAR_WIDTH, str(int(product.width)))
    if product.height:
        add(CHAR_HEIGHT, str(int(product.height)))
    if product.diameter:
        add(CHAR_DIAMETER, str(int(product.diameter)))

    add(CHAR_SEASON, SEASON_LABEL.get(product.season or ""))
    add(CHAR_THORN, "Да" if product.thorn else "Нет")
    add(CHAR_SPEED, a.get("speed_index"))
    add(CHAR_BRAND, product.brand)
    add(CHAR_VENDOR_CODE, product.cae)
    add(CHAR_PURPOSE, PURPOSE_LABEL.get(product.tyre_type or ""))

    # Индекс нагрузки у WB числовой, а 4tochki отдают «107/105» — берём первый.
    load = str(a.get("load_index") or "").split("/")[0].strip()
    if load.isdigit():
        add(CHAR_LOAD, int(load))

    # Шумность: «71dB» → 71.
    noise = "".join(ch for ch in str(product.noise or "") if ch.isdigit())
    if noise:
        add(CHAR_NOISE, int(noise))

    if product.strengthening:
        add(CHAR_RUNFLAT, "Нет")  # усиленность ≠ RunFlat; RunFlat 4tochki отдельно не отдают

    return chars


def build_card(product: Product, price: Decimal) -> dict:
    """Карточка для POST /content/v2/cards/upload.

    price — цена продажи с уже применённой наценкой, в рублях.
    """
    subject_id = SUBJECT_BY_TYPE.get(product.goods_type)
    if subject_id is None:
        raise CardBuildError(
            f"Тип товара «{product.goods_type}» не поддерживается: карточки создаются "
            "только для шин и дисков."
        )
    if not product.brand:
        raise CardBuildError("У товара не указан бренд — WB не примет карточку.")
    if price is None or price <= 0:
        raise CardBuildError("Нет цены поставщика — продавать нечего.")

    a = product.attrs or {}
    size = ""
    if product.width and product.height and product.diameter:
        size = f"{int(product.width)}/{int(product.height)} R{int(product.diameter)}"

    description = " ".join(
        part
        for part in (
            f"{product.brand} {product.model or ''}".strip(),
            size,
            f"Индекс нагрузки {a['load_index']}." if a.get("load_index") else "",
            f"Индекс скорости {a['speed_index']}." if a.get("speed_index") else "",
            f"Сезон: {SEASON_LABEL.get(product.season or '', '')}."
            if product.season
            else "",
            f"Камера: {product.camera}." if product.camera else "",
        )
        if part
    )

    return {
        "subjectID": subject_id,
        "variants": [
            {
                "vendorCode": vendor_code(product),
                # WB режет заголовок по 60 символов — обрезаем сами, иначе карточка
                # уйдёт в ошибку модерации целиком.
                "title": (product.name or f"{product.brand} {size}")[:60],
                "description": description[:2000],
                "brand": product.brand,
                "dimensions": _package_dims(product),
                "characteristics": _characteristics(product),
                "sizes": [
                    {
                        "techSize": "0",
                        "wbSize": "",
                        "price": int(price),
                        "skus": [barcode_for(product)],
                    }
                ],
            }
        ],
    }
