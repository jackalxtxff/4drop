"""Безопасный вычислитель формулы ценообразования.

Пользователь задаёт цену выражением с переменными, отдельно для WB и Ozon —
у площадок разные комиссии. Выражение считается через разбор AST с белым списком
операций и функций: НИКАКОГО eval, доступа к именам/атрибутам/вызовам нет, кроме
явно разрешённых. Иначе строка из настроек стала бы дырой на исполнение кода.

Доступные переменные:
  purchase  — закупочная цена 4tochki (₽)
  rrp       — рекомендованная розничная 4tochki (price_rozn, ₽); 0, если не пришла
  weight    — вес, кг (0, если неизвестен)
  price / wb_price / ozon_price — наша цена продажи на площадке (₽). Доступна ТОЛЬКО
            в формуле «цены до скидки»: она считается после основной цены. Все три
            имени — синонимы, чтобы формулу было привычно читать под конкретную площадку.

Доступные функции:
  round(x, n=0)      — обычное округление
  ceil(x) / floor(x) — вверх / вниз
  round_to(x, step)  — округлить вверх до кратного step (напр. round_to(x, 10) → до 10 ₽)
  min(...) / max(...)

Пример: round_to(purchase * 1.2 / (1 - 0.17), 10)
        наценка 20%, комиссия WB 17%, округление вверх до 10 ₽.
"""

from __future__ import annotations

import ast
import math
from decimal import Decimal

ALLOWED_VARS = ("purchase", "rrp", "weight", "price", "wb_price", "ozon_price")


class FormulaError(ValueError):
    """Формула некорректна или использует запрещённое."""


def _round_to(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.ceil(x / step) * step


_FUNCS = {
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "round_to": _round_to,
    "min": min,
    "max": max,
    "abs": abs,
}

# Разрешённые узлы AST. Всё, чего тут нет (атрибуты, индексация, лямбды,
# comprehension'ы, обращения к именам-функциям кроме вызова), — отклоняется.
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
)


def _validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise FormulaError(
                f"Недопустимая конструкция в формуле: {type(child).__name__}"
            )
        if isinstance(child, ast.Constant) and not isinstance(child.value, (int, float)):
            raise FormulaError("В формуле допустимы только числа.")
        if isinstance(child, ast.Call):
            if not isinstance(child.func, ast.Name) or child.func.id not in _FUNCS:
                raise FormulaError("Разрешены только функции: " + ", ".join(_FUNCS))
        if isinstance(child, ast.Name) and child.id not in ALLOWED_VARS and child.id not in _FUNCS:
            raise FormulaError(
                f"Неизвестная переменная «{child.id}». Доступны: {', '.join(ALLOWED_VARS)}"
            )


def compile_formula(expr: str) -> ast.Expression:
    """Разобрать и проверить формулу. Бросает FormulaError при проблеме."""
    if not expr or not expr.strip():
        raise FormulaError("Формула пустая.")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"Синтаксическая ошибка: {exc.msg}") from exc
    _validate(tree)
    return tree


def _eval(node: ast.AST, variables: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, variables)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        return float(variables.get(node.id, 0))
    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, variables)
        return -val if isinstance(node.op, ast.USub) else +val
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, variables), _eval(node.right, variables)
        op = node.op
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            if b == 0:
                raise FormulaError("Деление на ноль в формуле.")
            return a / b
        if isinstance(op, ast.FloorDiv):
            return a // b
        if isinstance(op, ast.Mod):
            return a % b
        if isinstance(op, ast.Pow):
            return a**b
    if isinstance(node, ast.Call):
        func = _FUNCS[node.func.id]
        args = [_eval(a, variables) for a in node.args]
        return float(func(*args))
    raise FormulaError("Формула содержит недопустимую конструкцию.")


def evaluate(
    tree: ast.Expression,
    purchase: Decimal | float | None,
    rrp: Decimal | float | None = None,
    weight: Decimal | float | None = None,
    price: Decimal | float | None = None,
) -> Decimal | None:
    """Вычислить цену. None на входе purchase → None (цены у поставщика нет).

    price — наша цена продажи на площадке; передаётся только при расчёте цены
    до скидки. wb_price/ozon_price — её синонимы.
    """
    if purchase is None:
        return None
    price_val = float(price or 0)
    variables = {
        "purchase": float(purchase),
        "rrp": float(rrp or 0),
        "weight": float(weight or 0),
        "price": price_val,
        "wb_price": price_val,
        "ozon_price": price_val,
    }
    try:
        result = _eval(tree, variables)
    except FormulaError:
        raise
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise FormulaError(f"Ошибка вычисления: {exc}") from exc

    if result is None or result < 0:
        return Decimal("0")
    return Decimal(str(result)).quantize(Decimal("0.01"))


def price_from_formula(
    expr: str,
    purchase: Decimal | None,
    rrp: Decimal | None = None,
    weight: Decimal | None = None,
    price: Decimal | None = None,
) -> Decimal | None:
    """Разово: скомпилировать и посчитать. Для пуска в цикле лучше compile_formula один раз."""
    return evaluate(compile_formula(expr), purchase, rrp, weight, price)
