"""A portfolio strategy: rank a universe on daily bars, hold the top N, rebalance on a schedule.

Momentum rotation is the reference case: rank F&O stocks by their 6-month
return, keep the ten strongest that are above their 200-day average, rebalance
monthly, and hold cash while the index is below its own 200-day average.

It is written in the same condition language as a single-instrument strategy
(indicators, operands, condition trees), so every piece of it is evaluated by
the same code the backtest and paper engines already trust.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.scan.spec import Universe
from backend.strategy.conditions import collect_exprs, collect_inline
from backend.strategy.schema import (
    PRICE_FIELDS,
    ConditionGroup,
    Expr,
    IndicatorSpec,
    InlineIndicator,
    Operand,
    operand_exprs,
    operand_refs,
)


class Regime(BaseModel):
    """When `condition` is false on `symbol` (a benchmark), the portfolio holds cash."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    condition: ConditionGroup

    @model_validator(mode="after")
    def _check(self) -> Regime:
        _check_refs("regime", self.indicators, self.condition.referenced_ids())
        return self


class PortfolioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    universe: Universe
    timeframe: Literal["1d"] = "1d"
    indicators: list[IndicatorSpec] = Field(default_factory=list)
    # What a stock must satisfy to be eligible on a rebalance day.
    filter: ConditionGroup | None = None
    # The value stocks are ranked by on a rebalance day; the highest are held.
    rank_by: Operand
    rank_order: Literal["desc", "asc"] = "desc"
    hold: int = Field(default=10, ge=1, le=50)
    rebalance: Literal["weekly", "monthly"] = "monthly"
    regime: Regime | None = None
    # Share of equity kept in cash on every rebalance, for fees and rounding.
    cash_buffer: float = Field(default=0.02, ge=0.0, le=0.5)

    @model_validator(mode="after")
    def _check(self) -> PortfolioSpec:
        used = set(operand_refs(self.rank_by))
        if self.filter is not None:
            used |= self.filter.referenced_ids()
        _check_refs("portfolio", self.indicators, used)
        return self

    def inline(self) -> list[InlineIndicator]:
        found = collect_inline(self.filter) if self.filter else []
        if isinstance(self.rank_by, InlineIndicator):
            found.append(self.rank_by)
        return found

    def exprs(self) -> list[Expr]:
        return (collect_exprs(self.filter) if self.filter else []) + operand_exprs(self.rank_by)

    def content_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()

    def family(self) -> str:
        """The shape of the idea with every number and the universe's members taken out.

        Like a strategy family (backend/strategy/family.py): retuning the
        lookback, the number held or the schedule is another try of the same
        idea, and counts toward its trials.
        """
        body = self.model_dump(mode="json", exclude={"name", "hold", "rebalance", "cash_buffer"})
        body["universe"] = "preset" if self.universe.preset else "list"
        text = re.sub(r"-?\d+(\.\d+)?", "#", json.dumps(body, sort_keys=True))
        return "pf_" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _check_refs(where: str, indicators: list[IndicatorSpec], used: set[str]) -> None:
    ids = [i.id for i in indicators]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{where}: duplicate indicator id(s) {sorted({i for i in ids if ids.count(i) > 1})}")
    # Price fields and declared indicators only: the streaming indicator sets a
    # portfolio runs on do not carry prior-day session levels.
    known = set(PRICE_FIELDS)
    for ind in indicators:
        known |= ind.reference_names()
    unknown = used - known
    if unknown:
        raise ValueError(f"{where}: unknown name(s) {sorted(unknown)}; declare them in `indicators` "
                         f"or use a price field ({', '.join(sorted(PRICE_FIELDS))}).")


def as_dict(spec: PortfolioSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json")
