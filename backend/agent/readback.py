"""Plain-English read-back of a strategy document, rendered by code.

This is what the user approves. It is generated from the validated document,
not written by the model, so it cannot describe something other than what will
run: if the model misunderstood, the read-back shows the misunderstanding.
"""

from __future__ import annotations

from typing import Any

from backend.strategy.schema import (
    Comparator,
    Condition,
    ConditionGroup,
    ExitRule,
    ExitType,
    Expr,
    ExprFn,
    InlineIndicator,
    Leg,
    LegKind,
    Strategy,
)

_CMP = {
    Comparator.GT: ">", Comparator.LT: "<", Comparator.GTE: ">=", Comparator.LTE: "<=",
    Comparator.EQ: "=", Comparator.NEQ: "!=",
    Comparator.CROSSES_ABOVE: "crosses above", Comparator.CROSSES_BELOW: "crosses below",
}

_EXPIRY = {
    "current_weekly": "current weekly expiry", "next_weekly": "next weekly expiry",
    "current_monthly": "current monthly expiry", "next_monthly": "next monthly expiry",
}


def _timeframe(tf: str) -> str:
    units = {"m": "minute", "h": "hour", "d": "day", "w": "week"}
    n, unit = tf[:-1] or "1", units.get(tf[-1], tf)
    return f"{n}-{unit} bars"


_ARITH = {ExprFn.ADD: "+", ExprFn.SUB: "-", ExprFn.MUL: "x", ExprFn.DIV: "/"}


def _ordinal(n: float) -> str:
    n = round(n, 2)
    text = f"{n:g}"
    if not float(n).is_integer():
        return f"{text}th"
    k = int(n)
    suffix = "th" if 10 <= k % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
    return f"{k}{suffix}"


def _operand(value: Any) -> str:
    if isinstance(value, Expr):
        if value.fn is ExprFn.RANK:
            return (f"the percentile rank (0 to 100) of {_operand(value.of)} "
                    f"among the previous {value.window} bars")
        if value.fn is ExprFn.QUANTILE:
            return (f"the {_ordinal((value.q or 0) * 100)} percentile of {_operand(value.of)} "
                    f"over the previous {value.window} bars")
        assert value.args is not None
        a, b = value.args
        return f"({_operand(a)} {_ARITH[value.fn]} {_operand(b)})"
    if isinstance(value, InlineIndicator):
        params = ", ".join(f"{v}" for v in value.params.values())
        inner = f"{value.of}{', ' + params if params else ''}"
        return f"{value.type}({inner})"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _condition(c: Condition | ConditionGroup, depth: int = 0) -> str:
    if isinstance(c, ConditionGroup):
        joiner = " AND " if c.op.value == "AND" else " OR "
        text = joiner.join(_condition(x, depth + 1) for x in c.conditions)
        return f"({text})" if depth and len(c.conditions) > 1 else text
    if c.cmp is Comparator.RISING:
        return f"{_operand(c.left)} is rising"
    if c.cmp is Comparator.FALLING:
        return f"{_operand(c.left)} is falling"
    return f"{_operand(c.left)} {_CMP[c.cmp]} {_operand(c.right)}"


def _exit(rule: ExitRule, what: str) -> str:
    if rule.type is ExitType.PERCENT:
        amount = f"{rule.value:g}%"
    elif rule.type is ExitType.POINTS:
        amount = f"{rule.value:g} points"
    else:
        amount = f"{rule.value:g} x {rule.ref}"
    return f"{what} {amount}"


def _strike(rule: str, right: str) -> str:
    """'OTM-2' on a put -> '2 strikes out of the money (OTM-2)': the words say
    what the engine does, since the sign on OTM/ITM rules is ignored."""
    import re

    upper = rule.upper()
    match = re.match(r"^(ATM|ITM|OTM)([+-]\d+)?$", upper)
    if not match:
        if upper.startswith("STRIKE:"):
            return f"{upper[7:]} strike"
        if upper.startswith("DELTA:"):
            return f"{upper[6:]}-delta"
        return rule
    kind, offset = match.group(1), int(match.group(2) or 0)
    if kind == "ATM":
        if offset == 0:
            return "at-the-money"
        return f"{abs(offset)} strike{'s' if abs(offset) != 1 else ''} {'above' if offset > 0 else 'below'} the money"
    n = abs(offset) or 1
    where = "out of the money" if kind == "OTM" else "in the money"
    return f"{n} strike{'s' if n != 1 else ''} {where}"


def _leg(leg: Leg) -> str:
    lots = f"{leg.lots} lot{'s' if leg.lots != 1 else ''}"
    if leg.kind is LegKind.OPTION and leg.option:
        right = "call" if leg.option.right.value == "CE" else "put"
        what = f"the {_strike(leg.option.strike_rule, leg.option.right.value)} {right} ({_EXPIRY[leg.option.expiry]})"
    elif leg.kind is LegKind.FUTURE:
        what = "the future"
    else:
        what = "the underlying"
    text = f"{leg.action.value} {lots} of {what}"
    exits = []
    basis = " of premium" if leg.kind is LegKind.OPTION else ""
    if leg.stop_loss:
        exits.append(_exit(leg.stop_loss, "stop loss"))
    if leg.target:
        exits.append(_exit(leg.target, "target"))
    if leg.trail:
        armed = f", armed after {leg.trail.activate_at:g}" if leg.trail.activate_at else ""
        exits.append(_exit(leg.trail, "trailing stop") + armed)
    return text + (f"; {', '.join(exits)}{basis}" if exits else "; no stop loss or target")


def render(strategy: Strategy) -> dict[str, Any]:
    """Sections for the approval card, each a heading and a list of lines."""
    s = strategy
    sections: list[dict[str, Any]] = []

    sections.append({"heading": "Market", "lines": [
        f"{s.instrument.symbol} ({s.instrument.segment}), {_timeframe(s.timeframe)}",
    ]})

    if s.is_intraday:
        timing = [
            f"Intraday: new entries between {s.session.start:%H:%M} and {s.session.end:%H:%M}",
            f"Every position is closed at {s.session.square_off:%H:%M}",
        ]
    else:
        timing = [
            f"Entries between {s.session.start:%H:%M} and {s.session.end:%H:%M}",
            "Positions are held overnight (no square-off time)",
        ]
    sections.append({"heading": "Timing", "lines": timing})

    if s.indicators:
        sections.append({"heading": "Indicators", "lines": [
            f"{i.id}: {i.type}(" + ", ".join(f"{k} {v}" for k, v in i.params.items()) + ")"
            for i in s.indicators
        ]})

    sections.append({"heading": "Enter when", "lines": [_condition(s.entry)]})
    sections.append({"heading": "Exit when", "lines": [
        _condition(s.exit) if s.exit else "No exit signal: exits come from stops, targets and timing",
    ]})
    sections.append({"heading": "Trades", "lines": [_leg(leg) for leg in s.legs]})

    risk = []
    if s.risk.max_loss_per_day:
        risk.append(f"Stop for the day after a loss of {s.risk.max_loss_per_day:,.0f}")
    if s.risk.max_profit_per_day:
        risk.append(f"Stop for the day after a profit of {s.risk.max_profit_per_day:,.0f}")
    if s.risk.max_trades_per_day:
        risk.append(f"At most {s.risk.max_trades_per_day} trade(s) a day")
    risk.append(f"At most {s.risk.max_concurrent_positions} position(s) open at once")
    if s.risk.re_entry.enabled:
        risk.append(
            f"Re-entry allowed up to {s.risk.re_entry.max} time(s), "
            f"{s.risk.re_entry.cooldown_bars} bar(s) apart"
        )
    sections.append({"heading": "Risk limits", "lines": risk})
    sections.append({"heading": "Costs", "lines": [
        f"Slippage {s.costs.slippage_bps:g} bps per fill, brokerage model {s.costs.brokerage_model}",
    ]})
    if s.sweep:
        from backend.strategy import sweep as sweep_module

        document = s.model_dump(mode="json")
        lines = [
            f"{sweep_module.path_label(axis.path, document)}: "
            + ", ".join(f"{v:g}" for v in axis.values)
            for axis in s.sweep
        ]
        total = sweep_module.count(s)
        lines.append(
            f"{total} combinations, each run as its own backtest and counted as a trial "
            "of this idea; a single backtest uses the values written above"
        )
        sections.append({"heading": "Sweep", "lines": lines})
    return {"title": s.name, "summary": s.summary(), "sections": sections}


def as_text(card: dict[str, Any]) -> str:
    out = [card["title"]]
    for section in card["sections"]:
        out.append(f"{section['heading']}:")
        out.extend(f"  - {line}" for line in section["lines"])
    return "\n".join(out)
