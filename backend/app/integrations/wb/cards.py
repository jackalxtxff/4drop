"""Сборка карточки Wildberries из товара 4tochki.

Все id ниже сняты с живого Content API (песочница), а не взяты из документации:
  * предмет 5283 «Шины автомобильные», 5284 «Диски колесные» (родитель «Шины и диски»);
  * характеристики — из GET /content/v2/object/charcs/5283.

Формально у предмета 5283 нет обязательных характеристик, но карточка без
типоразмера и сезонности бесполезна в поиске WB, поэтому заполняем всё, что есть
в данных поставщика.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from decimal import Decimal

from app.models import DEFAULT_VENDOR_PREFIX, Product

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
CHAR_VENDOR_CODE = 5522881  # Артикул производителя
CHAR_TNVED = 15000001      # ТНВЭД (строка; коды берутся из /content/v2/directory/tnved)

# Числовые характеристики (charcType=4 в Content API): значение уходит ГОЛЫМ числом,
# без обёртки в массив — иначе WB отвечает «Неправильный тип значения». Проверено на
# боевом кабинете через GET /content/v2/object/charcs/5283. Остальные (charcType=1) —
# строки в массиве. Бренд НЕ шлём характеристикой (14177446): как значение
# бренд-характеристики WB валидирует его по справочнику и отвечает «бренда нет на WB,
# добавьте новый»; бренд задаётся только верхнеуровневым полем variants[].brand.
NUMERIC_CHARS = frozenset({CHAR_LOAD, CHAR_NOISE})

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
# Значение настраивается на странице синхронизации (SyncSettings.vendor_prefix);
# здесь только дефолт для вызовов без явного префикса.
VENDOR_PREFIX = DEFAULT_VENDOR_PREFIX


def vendor_code(product: Product, prefix: str = VENDOR_PREFIX) -> str:
    """Артикул продавца (vendorCode) для карточки WB. Ключ сопоставления с нашей базой."""
    return f"{prefix}{product.cae}"


def resolve_wb_brand(
    brand: str | None,
    registry: dict[str, str] | None,
    manual_map: dict[str, str] | None = None,
) -> str | None:
    """Бренд каталога → каноничное написание бренда в реестре WB, или None если бренда
    в реестре категории нет.

    WB принимает бренд только в точном виде из своего реестра (GET /api/content/v1/brands),
    причём регистр у брендов разный: HANKOOK, но Yokohama, Kama, MICHELIN — угадывать
    нельзя. Поэтому сопоставляем без регистра с реальным реестром категории.
    manual_map — ручной override (высший приоритет) для случаев, когда имя бренда в
    4tochki не совпадает с WB (напр. «Galaxy (Yokohama ATG)»).

    registry=None означает «реестр недоступен» (в песочнице этого метода нет вовсе) —
    тогда отдаём бренд как есть, а не блокируем создание карточки. registry={} — это
    пустой реестр категории, и тогда None на выходе значит «бренда нет», карточку не
    собираем и сообщаем понятную причину.
    """
    if not brand:
        return None
    key = brand.strip().lower()
    if manual_map and key in manual_map:
        return manual_map[key]
    if registry is None:
        return brand
    return registry.get(key)


def is_ours(vendor_code_value: str | None, prefixes: Sequence[str] = (VENDOR_PREFIX,)) -> bool:
    """Наша ли это карточка — по префиксу vendorCode. Защита от правки чужих товаров.

    prefixes — текущий префикс плюс все, что использовались раньше: карточки, созданные
    до смены префикса, обязаны остаться «своими», иначе система завела бы им дубли.
    """
    return bool(vendor_code_value) and any(
        vendor_code_value.startswith(p) for p in prefixes if p
    )


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
        if char_id in NUMERIC_CHARS:
            # charcType=4: голое число, без массива.
            chars.append({"id": char_id, "value": value})
        else:
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
    # Бренд — только верхнеуровневым полем variants[].brand, не характеристикой.
    add(CHAR_VENDOR_CODE, product.cae)
    add(CHAR_PURPOSE, PURPOSE_LABEL.get(product.tyre_type or ""))
    # ТНВЭД: 4tochki отдаёт код, WB держит его характеристикой (строкой). Для шин
    # это код с обязательной маркировкой «Честный знак» (isKiz), но сам код WB принимает.
    add(CHAR_TNVED, str(product.tn_ved) if product.tn_ved else None)

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


def build_card(
    product: Product,
    price: Decimal,
    barcode: str,
    wb_brand: str,
    prefix: str = VENDOR_PREFIX,
) -> dict:
    """Карточка для POST /content/v2/cards/upload.

    price — цена продажи с уже применённой наценкой, в рублях.
    barcode — штрихкод (EAN), сгенерированный средствами WB (/content/v2/barcodes).
    Генерировать штрихкод вручную нельзя: WB требует свой валидный EAN, иначе остатки
    по нему потом не примутся.
    wb_brand — бренд в написании реестра WB (см. resolve_wb_brand). Идёт в поле brand;
    производитель (product.brand) при этом остаётся в названии и описании.
    """
    subject_id = SUBJECT_BY_TYPE.get(product.goods_type)
    if subject_id is None:
        raise CardBuildError(
            f"Тип товара «{product.goods_type}» не поддерживается: карточки создаются "
            "только для шин и дисков."
        )
    if not wb_brand:
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

    variant = {
        "vendorCode": vendor_code(product, prefix),
        # WB режет заголовок по 60 символов — обрезаем сами, иначе карточка
        # уйдёт в ошибку модерации целиком.
        "title": (product.name or f"{product.brand} {size}")[:60],
        "description": description[:2000],
        "brand": wb_brand,
        "dimensions": _package_dims(product),
        "characteristics": _characteristics(product),
        "sizes": [
            {
                "techSize": "0",
                "wbSize": "",
                "price": int(price),
                "skus": [barcode],
            }
        ],
    }
    card = {"subjectID": subject_id, "variants": [variant]}

    # 289-ФЗ: подтверждаем, что товар маркирован средствами идентификации («Честный
    # знак»). Без флага WB со временем блокирует карточку. По спеке WB kizMarked —
    # свойство варианта (рядом с vendorCode), и в /cards/upload, и в /cards/update.
    # Шины подлежат обязательной маркировке; диски — нет, поэтому ставим только шинам.
    if product.goods_type == "tyre":
        variant["kizMarked"] = True

    return card


def card_content_hash(product: Product) -> str:
    """Хэш атрибутивной части карточки — без цены и остатка.

    Цена меняется часто и уходит отдельным пушем, поэтому в хэш не входит: иначе
    любое изменение цены гнало бы карточку на повторную модерацию. Считаем от того
    же, что уходит в build_card, но с обнулённой ценой и выкинутым блоком sizes.
    """
    # Штрихкод в хэш не входит (блок sizes удаляем). Бренд для хэша берём исходный
    # (детерминированно): смена карты брендов не должна гнать все карточки на переобновление.
    card = build_card(product, Decimal("1"), "", product.brand or "")
    # kizMarked применяется только при создании (update его не пишет), поэтому из хэша
    # исключаем — иначе он вызвал бы бессмысленную перемодерацию всех старых карточек.
    card.pop("kizMarked", None)
    for variant in card.get("variants", []):
        variant.pop("sizes", None)
        variant.pop("kizMarked", None)
    payload = json.dumps(card, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()
