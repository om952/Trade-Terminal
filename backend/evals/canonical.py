"""A canonical form for strategy documents, so meaning is compared, not spelling.

Two documents that trade identically must compare equal even when the model
named its indicators differently, listed conditions in another order, wrote
`a < b` where the gold has `b > a`, or left a default implicit. Everything the
scorer compares goes through here first.

What is deliberately NOT normalized: numeric values, comparators that change
meaning (> versus >=), option strikes that resolve to different contracts.
"""

from __future__ import annotations

import re
from typing import Any

from backend.data.upstox_client import Timeframe
from backend.indicators import registry
from backend.strategy.schema import (
    Comparator,
    Condition,
    ConditionGroup,
    ExitRule,
    InlineIndicator,
    Leg,
    Strategy,
)

ALWAYS = ("always",)

_FLIP = {
    Comparator.LT: Comparator.GT, Comparator.LTE: Comparator.GTE,
    Comparator.CROSSES_BELOW: Comparator.CROSSES_ABOVE,
}
_SYMMETRIC = {Comparator.EQ, Comparator.NEQ}


def _num(value: float | int) -> str:
    return f"{float(value):g}"


def signature(type_name: str, params: dict[str, Any]) -> str:
    """'EMA(period=9,source=close)': the indicator with every default filled in."""
    spec = registry.get(type_name)
    merged = {**spec.params, **params}
    inner = ",".join(
        f"{k}={_num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v).lower()}"
        for k, v in sorted(merged.items())
    )
    return f"{spec.name.upper()}({inner})"


def _default_output(type_name: str) -> str:
    outputs = registry.get(type_name).outputs
    return outputs[0] if outputs else "value"


def operand(strategy: Strategy, value: Any) -> str:
    if isinstance(value, InlineIndicator):
        params = dict(value.params)
        if "source" in registry.get(value.type).params:
            params.setdefault("source", value.of)
        return f"{signature(value.type, params)}.{_default_output(value.type)}"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return _num(value)
    text = str(value)
    ind_id, _, output = text.partition(".")
    spec = strategy.indicator_by_id(ind_id)
    if spec is not None:
        return f"{signature(spec.type, spec.params)}.{output or _default_output(spec.type)}"
    return text.lower()


def condition(strategy: Strategy, c: Condition | ConditionGroup) -> Any:
    if isinstance(c, ConditionGroup):
        return group(strategy, c)
    left, right, cmp = operand(strategy, c.left), operand(strategy, c.right), c.cmp
    if cmp in (Comparator.RISING, Comparator.FALLING):
        return (cmp.value, left)
    if cmp in _FLIP:
        left, right, cmp = right, left, _FLIP[cmp]
    if cmp in _SYMMETRIC and right < left:
        left, right = right, left
    if (left, cmp, right) in (("close", Comparator.GT, "0"), ("close", Comparator.GTE, "0")):
        return ALWAYS           # the time-based-entry convention
    return (left, cmp.value, right)


def group(strategy: Strategy, g: ConditionGroup | None) -> Any:
    """Flattened, order-insensitive: AND(a, AND(b, c)) == AND(c, b, a)."""
    if g is None:
        return None
    children: list[Any] = []
    for child in g.conditions:
        canon = condition(strategy, child)
        if isinstance(canon, tuple) and canon and canon[0] == g.op.value and isinstance(child, ConditionGroup):
            children.extend(canon[1])
        else:
            children.append(canon)
    children = sorted(set(children), key=repr)
    if len(children) == 1:
        return children[0]
    return (g.op.value, tuple(children))


def strike(rule: str) -> str:
    """Rules that resolve to the same contract get the same text."""
    upper = rule.upper().strip()
    match = re.match(r"^(ATM|ITM|OTM)([+-]\d+)?$", upper)
    if match:
        kind, offset = match.group(1), int(match.group(2) or 0)
        if kind == "ATM":
            return f"ATM{offset:+d}"
        return f"{kind}+{abs(offset) or 1}"      # the sign is ignored by the engine
    if upper.startswith(("STRIKE:", "DELTA:")):
        head, _, number = upper.partition(":")
        return f"{head}:{_num(float(number))}"
    return upper


def _exit(strategy: Strategy, rule: ExitRule | None) -> Any:
    if rule is None:
        return None
    ref = operand(strategy, rule.ref) if rule.ref else None
    out = (rule.type.value, _num(rule.value), ref)
    activate = getattr(rule, "activate_at", None)
    return (*out, _num(activate)) if activate is not None else out


def leg(strategy: Strategy, item: Leg) -> tuple:
    option = item.option
    return (
        item.action.value, item.kind.value, item.lots,
        option.right.value if option else None,
        strike(option.strike_rule) if option else None,
        option.expiry if option else None,
        _exit(strategy, item.stop_loss), _exit(strategy, item.target), _exit(strategy, item.trail),
    )


def timeframe(label: str) -> str:
    tf = Timeframe.parse(label)
    if tf.unit == "hours":
        return f"{tf.interval * 60}m"
    return tf.label


def canonical(strategy: Strategy) -> dict[str, Any]:
    """Every field the scorer compares, in comparable form."""
    from backend.agent.graph import resolve_symbol

    symbol, _ = resolve_symbol(strategy.instrument.symbol)
    risk = strategy.risk.model_dump(mode="json")
    return {
        "instrument": symbol,
        "timeframe": timeframe(strategy.timeframe),
        "session": {
            "intraday": strategy.is_intraday,
            "start": strategy.session.start.strftime("%H:%M"),
            "end": strategy.session.end.strftime("%H:%M"),
            "square_off": strategy.session.square_off.strftime("%H:%M")
            if strategy.session.square_off else None,
        },
        "entry": group(strategy, strategy.entry),
        "exit": group(strategy, strategy.exit),
        "legs": sorted((leg(strategy, item) for item in strategy.legs), key=repr),
        "risk": {**{k: v for k, v in risk.items() if k != "re_entry"},
                 **{f"re_entry.{k}": v for k, v in risk["re_entry"].items()}},
    }
