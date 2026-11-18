"""Hold the explanation to the computed numbers.

The model explains a backtest it did not run. It is given the computed figures
and may only quote them; any other number in its text (an invented win rate, a
misremembered drawdown) is caught here and the explanation is not shown as is.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Times, dates and indicator-style tokens are not quantities.
_IGNORE = re.compile(
    r"\b\d{1,2}:\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\b[A-Za-z]+\d+\b|\b\d+(?:m|h|d|w)\b"
)
_NUMBER = re.compile(r"(?<![\w.])[-+−]?(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?")

# Counting words in prose ("two caveats", "one trade") are not checked;
# small integers written as digits are, because "1 trade" is a claim.
_EXEMPT = {0.0}


def _facts(value: Any, out: list[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            out.append(float(value))
    elif isinstance(value, dict):
        for v in value.values():
            _facts(v, out)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _facts(v, out)
    elif isinstance(value, str):
        for m in _NUMBER.finditer(_IGNORE.sub(" ", value)):
            out.append(_parse(m.group()))


def _parse(text: str) -> float:
    return float(text.replace(",", "").replace("−", "-").lstrip("+"))


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def unsupported_numbers(text: str, facts: Any) -> list[str]:
    """Numbers in `text` that no fact supports, allowing for rounding and sign."""
    known: list[float] = []
    _facts(facts, known)
    bad = []
    for m in _NUMBER.finditer(_IGNORE.sub(" ", text)):
        raw = m.group()
        value = _parse(raw)
        if abs(value) in _EXEMPT:
            continue
        # Rounded to the text's precision; generous for whole numbers quoted
        # from large values ("about 12,400" for 12,398.7).
        tol = 0.5 * 10 ** -_decimals(raw)
        ok = any(
            abs(abs(value) - abs(k)) <= max(tol, 0.0051 * abs(k) if _decimals(raw) == 0 and abs(k) >= 1000 else tol)
            or abs(abs(value) - abs(k) * 100) <= tol        # a fraction quoted as a percentage
            for k in known
        )
        if not ok:
            bad.append(raw)
    return bad
