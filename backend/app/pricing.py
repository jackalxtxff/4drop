"""Наценка: закупочная × (1 + percent/100) + fixed, затем округление вверх.

Правила берутся из pricing_rules. Более специфичное правило (бренд + тип товара)
переопределяет менее специфичное; при равной специфичности решает priority.
"""

from decimal import ROUND_CEILING, Decimal

from app.models import PricingRule, Product


def _specificity(rule: PricingRule) -> tuple[int, int]:
    """Чем больше заполненных измерений, тем специфичнее правило."""
    score = sum(1 for f in (rule.brand, rule.goods_type, rule.platform) if f)
    return (score, rule.priority)


def select_rule(
    rules: list[PricingRule],
    product: Product,
    platform: str | None = None,
) -> PricingRule | None:
    applicable = [
        r
        for r in rules
        if (r.brand is None or r.brand == product.brand)
        and (r.goods_type is None or r.goods_type == product.goods_type)
        and (r.platform is None or r.platform == platform)
    ]
    if not applicable:
        return None
    return max(applicable, key=_specificity)


def apply_markup(purchase_price: Decimal, rule: PricingRule | None) -> Decimal | None:
    """Цена продажи из закупочной. None на входе — цены у поставщика нет, продавать нечего."""
    if purchase_price is None:
        return None
    if rule is None:
        return purchase_price

    price = purchase_price * (Decimal("1") + rule.percent / Decimal("100")) + rule.fixed

    if rule.round_to and rule.round_to > 0:
        # Округляем вверх: округление вниз режет маржу на каждой продаже.
        price = (price / rule.round_to).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        ) * rule.round_to

    if rule.min_price is not None and price < rule.min_price:
        price = rule.min_price

    return price.quantize(Decimal("0.01"))
