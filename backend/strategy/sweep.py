"""Expanding a strategy's `sweep` into the concrete documents a grid runs.

A sweep never changes what a single run of the document does: the document
holds concrete numbers, and the sweep lists which of them to vary. Expansion
replaces those numbers, drops `sweep`, and validates each variant like any
other document. A variant that does not validate (a period of 0, a stop of -1)
is reported as skipped with the reason, rather than failing the whole grid.

Each variant is an ordinary strategy with its own content hash, and all of
them share one family (numbers are not part of a family's shape). Every
variant that runs is recorded, so the family's trial count reflects the whole
grid: that count is what the deflated Sharpe in Phase 4 corrects for.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from backend.strategy.schema import Strategy, pointer_get, pointer_set


@dataclass
class Variant:
    assignment: dict[str, int | float]
    strategy: Strategy | None = None
    error: str | None = None
    label: str = field(default="")


def _coerce(original: Any, value: int | float) -> int | float:
    """Keep an integer field an integer when the swept value is a whole number."""
    if isinstance(original, int) and float(value).is_integer():
        return int(value)
    return value


def path_label(path: str, document: dict[str, Any] | None = None) -> str:
    """'/indicators/0/params/period' -> 'ema_fast.period' (or 'indicators.0.period'
    without the document): indicators and legs are named by their ids."""
    tokens = path.strip("/").split("/")
    if document and len(tokens) > 1 and tokens[0] in ("indicators", "legs") and tokens[1].isdigit():
        item = pointer_get(document, f"/{tokens[0]}/{tokens[1]}")
        if isinstance(item, dict) and item.get("id"):
            tokens = [item["id"], *tokens[2:]]
    return ".".join(t for t in tokens if t != "params")


def label(assignment: dict[str, int | float], document: dict[str, Any] | None = None) -> str:
    """{'/indicators/0/params/period': 9} -> 'ema_fast.period=9'."""
    return ", ".join(
        f"{path_label(path, document)}={value:g}" for path, value in assignment.items()
    )


def count(strategy: Strategy) -> int:
    total = 1
    for axis in strategy.sweep:
        total *= len(axis.values)
    return total if strategy.sweep else 0


def variants(strategy: Strategy) -> list[Variant]:
    """Every combination of the sweep's values, in a stable order."""
    if not strategy.sweep:
        return []
    from backend.services.backtest import format_validation_error

    base = strategy.model_dump(mode="json", exclude={"sweep", "id", "schema_version"})
    paths = [axis.path for axis in strategy.sweep]
    out: list[Variant] = []
    for values in itertools.product(*(axis.values for axis in strategy.sweep)):
        document = _deep_copy(base)
        assignment: dict[str, int | float] = {}
        for path, value in zip(paths, values):
            value = _coerce(pointer_get(base, path), value)
            pointer_set(document, path, value)
            assignment[path] = value
        variant = Variant(assignment=assignment, label=label(assignment, base))
        try:
            variant.strategy = Strategy.model_validate(document)
        except Exception as exc:  # noqa: BLE001 - reported per variant
            errors = format_validation_error(exc)
            variant.error = "; ".join(f"{e['field']}: {e['message']}" for e in errors)
        out.append(variant)
    return out


def _deep_copy(document: Any) -> Any:
    if isinstance(document, dict):
        return {k: _deep_copy(v) for k, v in document.items()}
    if isinstance(document, list):
        return [_deep_copy(v) for v in document]
    return document
