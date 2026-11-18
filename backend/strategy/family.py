"""Strategy families: the unit that multiple-testing corrections count trials in.

A family is a strategy's logic with every tunable value taken out. Two documents
are in the same family when they differ only in things a trader would sweep
while hunting for a good backtest:

* numbers: indicator periods, thresholds, stop and target sizes, lots, limits;
* times: the entry window and the square-off time;
* strike offsets (ATM+1 versus ATM+2) and the expiry series;
* an expression's window and quantile (rank of RSI over 50 or 100 bars);
* the sweep itself: its variants are the trials this module counts;
* the instrument and the timeframe;
* names, ids and descriptions.

What stays is the shape of the idea: which indicators, what they are computed
on, how conditions compare and combine, which way each leg trades, which kind
of contract, which exits and risk rules exist (not their values), and whether
it is intraday.

Why this definition: deflated Sharpe (PRODUCTION_PLAN.md P4.2) needs the number
of variants tried before one was kept. Retuning an EMA from 9 to 11 is another
draw from the same family; a 9/21 EMA crossover with an RSI filter is a
different idea. Instrument and timeframe count as parameters because searching
across them is the same kind of selection. Counting generously only makes the
later correction more conservative, which is the safe direction.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from backend.indicators import registry
from backend.strategy.schema import (
    Comparator,
    Condition,
    ConditionGroup,
    ExitRule,
    Expr,
    ExprFn,
    InlineIndicator,
    Leg,
    Strategy,
)

NUMBER = "#"

_FLIP = {
    Comparator.LT: Comparator.GT, Comparator.LTE: Comparator.GTE,
    Comparator.CROSSES_BELOW: Comparator.CROSSES_ABOVE,
}


def _shape(type_name: str, params: dict[str, Any]) -> str:
    """'EMA[source=close]': the indicator with its numeric parameters removed.

    String parameters stay: an EMA of volume is a different idea from an EMA of
    the close, while an EMA of period 9 and one of period 21 are not.
    """
    spec = registry.get(type_name)
    merged = {**spec.params, **params}
    text = ",".join(
        f"{k}={str(v).lower()}" for k, v in sorted(merged.items())
        if isinstance(v, str)
    )
    return f"{spec.name.upper()}[{text}]"


def _default_output(type_name: str) -> str:
    outputs = registry.get(type_name).outputs
    return outputs[0] if outputs else "value"


_COMMUTATIVE = frozenset({ExprFn.ADD, ExprFn.MUL})


def _operand(strategy: Strategy, value: Any) -> str:
    if isinstance(value, Expr):
        # The function and what it reads are the idea; windows and quantiles
        # are tuning, like an indicator's period.
        if value.is_rolling:
            return f"{value.fn.value}({_operand(strategy, value.of)})"
        args = [_operand(strategy, a) for a in value.args or ()]
        if value.fn in _COMMUTATIVE:
            args.sort()
        return f"{value.fn.value}({','.join(args)})"
    if isinstance(value, InlineIndicator):
        params = dict(value.params)
        if "source" in registry.get(value.type).params:
            params.setdefault("source", value.of)
        return f"{_shape(value.type, params)}.{_default_output(value.type)}"
    if isinstance(value, (bool, int, float)):
        return NUMBER
    text = str(value)
    ind_id, _, output = text.partition(".")
    spec = strategy.indicator_by_id(ind_id)
    if spec is not None:
        return f"{_shape(spec.type, spec.params)}.{output or _default_output(spec.type)}"
    return text.lower()                       # price and context fields


def _condition(strategy: Strategy, c: Condition | ConditionGroup) -> Any:
    if isinstance(c, ConditionGroup):
        return _group(strategy, c)
    left, right, cmp = _operand(strategy, c.left), _operand(strategy, c.right), c.cmp
    if cmp in (Comparator.RISING, Comparator.FALLING):
        return [cmp.value, left]
    if cmp in _FLIP:
        left, right, cmp = right, left, _FLIP[cmp]
    if cmp in (Comparator.EQ, Comparator.NEQ) and right < left:
        left, right = right, left
    return [left, cmp.value, right]


def _group(strategy: Strategy, g: ConditionGroup | None) -> Any:
    if g is None:
        return None
    children: list[Any] = []
    for child in g.conditions:
        canon = _condition(strategy, child)
        nested = isinstance(child, ConditionGroup) and isinstance(canon, dict)
        if nested and canon["op"] == g.op.value:
            children.extend(canon["of"])
        else:
            children.append(canon)
    unique = sorted({json.dumps(c, sort_keys=True) for c in children})
    children = [json.loads(c) for c in unique]
    if len(children) == 1:
        return children[0]
    return {"op": g.op.value, "of": children}


def _strike_kind(rule: str) -> str:
    match = re.match(r"^(ATM|ITM|OTM|STRIKE|DELTA)", rule.upper())
    return match.group(1) if match else rule.upper()


def _exit(rule: ExitRule | None) -> str | None:
    return None if rule is None else rule.type.value


def _leg(item: Leg) -> list[Any]:
    option = item.option
    return [
        item.action.value, item.kind.value,
        option.right.value if option else None,
        _strike_kind(option.strike_rule) if option else None,
        _exit(item.stop_loss), _exit(item.target), _exit(item.trail),
    ]


def describe(strategy: Strategy) -> dict[str, Any]:
    """The family's defining shape, as plain JSON."""
    risk = strategy.risk
    rules = sorted(
        name for name, value in (
            ("max_loss_per_day", risk.max_loss_per_day),
            ("max_profit_per_day", risk.max_profit_per_day),
            ("max_trades_per_day", risk.max_trades_per_day),
        ) if value is not None
    )
    if risk.re_entry.enabled:
        rules.append("re_entry")
    legs = sorted((_leg(item) for item in strategy.legs), key=lambda x: json.dumps(x))
    return {
        "intraday": strategy.is_intraday,
        "entry": _group(strategy, strategy.entry),
        "exit": _group(strategy, strategy.exit),
        "legs": legs,
        "risk_rules": rules,
    }


def family_key(strategy: Strategy | dict[str, Any]) -> str:
    """A short, stable id for the strategy's family."""
    if not isinstance(strategy, Strategy):
        strategy = Strategy.model_validate(strategy)
    text = json.dumps(describe(strategy), sort_keys=True, separators=(",", ":"))
    return "fam_" + hashlib.sha256(text.encode()).hexdigest()[:16]
