"""Condition evaluation — turning the JSON boolean tree into a yes/no per bar.

Two rules govern everything here:

**NaN is never true.** During warm-up an indicator has no value. A comparison
against NaN returns False rather than propagating, so a strategy cannot fire on
the opening bars before its indicators mean anything. This is the difference
between a backtest that starts trading on bar 1 with garbage signals and one
that waits.

**Crossovers need the previous bar, not the previous signal.** `crosses_above`
is true only on the bar where the relationship flips. Evaluating it from current
values alone would make it identical to `>`, and every crossover strategy would
degenerate into a permanently-long one.
"""

from __future__ import annotations

import math
from typing import Union

from backend.strategy.indicator_set import IndicatorSet, inline_key
from backend.strategy.schema import (
    BoolOp,
    Comparator,
    Condition,
    ConditionGroup,
    InlineIndicator,
    Operand,
)

# Floating-point equality on prices is meaningless; treat near-equal as equal.
EPSILON = 1e-9


def _operand_value(operand: Operand, indicators: IndicatorSet, previous: bool = False) -> float:
    """Resolve an operand to a number for the current (or previous) bar."""
    if isinstance(operand, (int, float)):
        return float(operand)

    if isinstance(operand, InlineIndicator):
        key = inline_key(operand)
        return indicators.previous(key) if previous else indicators.value(key)

    if isinstance(operand, str):
        return indicators.previous(operand) if previous else indicators.value(operand)

    raise TypeError(f"Unsupported operand type: {type(operand).__name__}")


def evaluate_condition(condition: Condition, indicators: IndicatorSet) -> bool:
    """Evaluate one comparison against the current bar."""
    left = _operand_value(condition.left, indicators)
    right = _operand_value(condition.right, indicators)

    if math.isnan(left) or math.isnan(right):
        return False

    cmp = condition.cmp

    if cmp is Comparator.GT:
        return left > right
    if cmp is Comparator.LT:
        return left < right
    if cmp is Comparator.GTE:
        return left >= right
    if cmp is Comparator.LTE:
        return left <= right
    if cmp is Comparator.EQ:
        return abs(left - right) <= EPSILON
    if cmp is Comparator.NEQ:
        return abs(left - right) > EPSILON

    if cmp in (Comparator.RISING, Comparator.FALLING):
        # `right` is ignored — slope is a property of `left` alone. The schema
        # still requires a right operand, so a document stays uniform; convention
        # is to write 0.
        prev = _operand_value(condition.left, indicators, previous=True)
        if math.isnan(prev):
            return False
        return left > prev if cmp is Comparator.RISING else left < prev

    if cmp in (Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW):
        prev_left = _operand_value(condition.left, indicators, previous=True)
        prev_right = _operand_value(condition.right, indicators, previous=True)
        if math.isnan(prev_left) or math.isnan(prev_right):
            return False

        if cmp is Comparator.CROSSES_ABOVE:
            return prev_left <= prev_right and left > right
        return prev_left >= prev_right and left < right

    raise ValueError(f"Unhandled comparator {cmp!r}")


def evaluate(node: Union[Condition, ConditionGroup], indicators: IndicatorSet) -> bool:
    """Evaluate a condition or a nested boolean group."""
    if isinstance(node, Condition):
        return evaluate_condition(node, indicators)

    if node.op is BoolOp.AND:
        return all(evaluate(child, indicators) for child in node.conditions)
    return any(evaluate(child, indicators) for child in node.conditions)


def explain(node: Union[Condition, ConditionGroup], indicators: IndicatorSet) -> dict:
    """Evaluate while recording every operand value.

    Used by the UI's "why did this fire?" view and when debugging a strategy that
    is not trading. Being able to see that `rsi` was NaN, rather than guessing,
    saves a lot of time.
    """
    if isinstance(node, Condition):
        left = _operand_value(node.left, indicators)
        right = _operand_value(node.right, indicators)
        entry = {
            "left": _describe(node.left),
            "left_value": None if math.isnan(left) else round(left, 6),
            "cmp": node.cmp.value,
            "right": _describe(node.right),
            "right_value": None if math.isnan(right) else round(right, 6),
            "result": evaluate_condition(node, indicators),
        }
        if node.cmp in (
            Comparator.CROSSES_ABOVE, Comparator.CROSSES_BELOW,
            Comparator.RISING, Comparator.FALLING,
        ):
            pl = _operand_value(node.left, indicators, previous=True)
            pr = _operand_value(node.right, indicators, previous=True)
            entry["prev_left"] = None if math.isnan(pl) else round(pl, 6)
            entry["prev_right"] = None if math.isnan(pr) else round(pr, 6)
        return entry

    return {
        "op": node.op.value,
        "result": evaluate(node, indicators),
        "conditions": [explain(child, indicators) for child in node.conditions],
    }


def _describe(operand: Operand) -> str:
    if isinstance(operand, InlineIndicator):
        params = ", ".join(f"{k}={v}" for k, v in sorted(operand.params.items()))
        return f"{operand.type}({operand.of}{', ' + params if params else ''})"
    return str(operand)


def collect_inline(node: Union[Condition, ConditionGroup, None]) -> list[InlineIndicator]:
    """Find every inline indicator in a tree so the IndicatorSet can prepare them."""
    if node is None:
        return []

    if isinstance(node, Condition):
        return [op for op in (node.left, node.right) if isinstance(op, InlineIndicator)]

    out: list[InlineIndicator] = []
    for child in node.conditions:
        out.extend(collect_inline(child))
    return out
