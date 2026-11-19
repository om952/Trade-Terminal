"""A scan: one condition tree, in the strategy language, run across a universe.

A scan asks "where is this true, and since when?" of many instruments at once.
It reuses the strategy's indicators, operands and condition tree unchanged, so
a condition that works in a backtest means the same thing in a scan, and a scan
can later become a strategy's entry without translation.

Daily bars only for now (P3.1): intraday across a universe is a rate-limit
problem (P3.3) that waits for the prefetch job and the live feed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.strategy.conditions import collect_exprs, collect_inline
from backend.strategy.schema import (
    PRICE_FIELDS,
    Condition,
    ConditionGroup,
    Expr,
    IndicatorSpec,
    InlineIndicator,
)

MAX_UNIVERSE = 500
HORIZONS = (1, 5, 20)

# Context a daily scan can use. Position fields mean nothing without a
# position, and a session VWAP over one daily bar is just its typical price.
SCAN_CONTEXT = {
    "pivot", "bc", "tc", "r1", "r2", "r3", "s1", "s2", "s3", "cpr_width",
    "prev_day_high", "prev_day_low", "prev_day_close",
}

Preset = Literal["fno_stocks"]


class Universe(BaseModel):
    """Either an explicit list of instruments or a named preset, not both."""

    model_config = ConfigDict(extra="forbid")

    symbols: list[str] = Field(default_factory=list, max_length=MAX_UNIVERSE)
    preset: Preset | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Universe:
        self.symbols = [s.strip() for s in self.symbols if s.strip()]
        if bool(self.symbols) == bool(self.preset):
            raise ValueError("give either symbols or a preset")
        if len(set(self.symbols)) != len(self.symbols):
            dupes = sorted({s for s in self.symbols if self.symbols.count(s) > 1})
            raise ValueError(f"symbols listed twice: {dupes}")
        return self


class ScanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    universe: Universe
    timeframe: Literal["1d"] = "1d"
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    condition: ConditionGroup
    # A label for the trader (which way the line is meant to break). It changes
    # nothing in the evaluation; the condition already says what is true.
    side: Literal["long", "short"] | None = None
    # How many recent bars to look through for when the condition last held.
    lookback: int = Field(default=20, ge=1, le=250)

    @model_validator(mode="after")
    def _check(self) -> ScanSpec:
        ids = [i.id for i in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate indicator id(s): {sorted({i for i in ids if ids.count(i) > 1})}")
        known = set(PRICE_FIELDS) | SCAN_CONTEXT
        for ind in self.indicators:
            known |= ind.reference_names()
        unknown = self.condition.referenced_ids() - known
        if unknown:
            raise ValueError(
                f"Condition references unknown name(s) {sorted(unknown)}. Declare them in "
                f"`indicators`, or use a price field ({', '.join(sorted(PRICE_FIELDS))}) or a "
                f"prior-day level ({', '.join(sorted(SCAN_CONTEXT))})."
            )
        return self

    def inline(self) -> list[InlineIndicator]:
        return collect_inline(self.condition)

    def exprs(self) -> list[Expr]:
        return collect_exprs(self.condition)

    def single_comparison(self) -> Condition | None:
        """The condition when it is one comparison: the only case a distance makes sense."""
        if len(self.condition.conditions) == 1 and isinstance(self.condition.conditions[0], Condition):
            return self.condition.conditions[0]
        return None

    def content_hash(self) -> str:
        body = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
