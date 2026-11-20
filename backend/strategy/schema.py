"""The strategy document — the single most important artifact in the system.

A strategy is *data*, not code. The visual builder is an editor over this schema;
the engine is an interpreter of it. That separation is what lets the same
definition drive a backtest, a paper session and live trading with no
translation step where bugs creep in.

Every document is content-hashed. Backtest results and live trades record the
hash, so a result can always be traced to the exact definition that produced it —
including when you edit a strategy and forget you did.

Validation is deliberately strict. A typo in an indicator reference should fail
at load time with a clear message, not silently evaluate to NaN and produce a
strategy that never trades while looking like it works.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time
from enum import Enum
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.interfaces import Side
from backend.indicators import registry

# Version 2 is a strict superset of version 1: it adds expression operands
# (`Expr`) and parameter sweeps (`sweep`). A document records the lowest version
# that can express it, so every v1 document keeps its exact content hash, its id
# and its family, and every stored result stays traceable to it. A document that
# claims version 1 but uses a v2 feature is refused rather than silently upgraded.
SCHEMA_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

# Bounds on a sweep. A grid is a set of backtests, each one recorded as a trial
# of the strategy's family, so its size is capped where the job stays minutes
# long and the multiple-testing penalty stays meaningful.
MAX_SWEEP_AXES = 4
MAX_SWEEP_VALUES = 50
MAX_SWEEP_RUNS = 200

PRICE_FIELDS = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4"}

# Session-scoped values the engine injects; usable in conditions like any indicator.
CONTEXT_FIELDS = {
    "pivot", "bc", "tc", "r1", "r2", "r3", "s1", "s2", "s3", "cpr_width",
    "prev_day_high", "prev_day_low", "prev_day_close",
    "vwap", "bars_since_entry", "position_pnl_pct", "entry_price",
}


# NOTE: `Side` is imported from core.interfaces, never redefined here.
#
# It was briefly defined twice — once here and once there — with identical
# members. `==` compared equal, so nothing looked wrong, but `is` did not, and
# `Position.apply_fill` uses `is` to decide direction. The result was that every
# leg with action=BUY silently opened a SHORT position and every backtest
# reported the exact inverse of the truth.
#
# One definition. Enums that cross module boundaries must have exactly one home.


class LegKind(str, Enum):
    EQUITY = "equity"
    FUTURE = "future"
    OPTION = "option"


class OptionRight(str, Enum):
    CE = "CE"
    PE = "PE"


class Comparator(str, Enum):
    GT = ">"
    LT = "<"
    GTE = ">="
    LTE = "<="
    EQ = "=="
    NEQ = "!="
    CROSSES_ABOVE = "crosses_above"
    CROSSES_BELOW = "crosses_below"
    # State, not event: is this value higher/lower than it was last bar?
    # "RSI above 50 and rising" needs both a level test and a slope test, and
    # without these the slope half can only be faked with an inline indicator
    # over another indicator — which the streaming engine cannot evaluate, so it
    # would back-test fine and never fire live.
    RISING = "rising"
    FALLING = "falling"


class BoolOp(str, Enum):
    AND = "AND"
    OR = "OR"


class ExitType(str, Enum):
    PERCENT = "percent"
    POINTS = "points"
    ATR_MULTIPLE = "atr_multiple"


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

class IndicatorSpec(BaseModel):
    """One indicator instance, referenced by `id` from conditions."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    type: str
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_registry(self) -> IndicatorSpec:
        try:
            spec = registry.get(self.type)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc

        unknown = set(self.params) - set(spec.params)
        if unknown:
            raise ValueError(
                f"Indicator {self.id} ({self.type}): unknown parameter(s) {sorted(unknown)}. "
                f"Valid: {sorted(spec.params)}"
            )
        return self

    @property
    def outputs(self) -> tuple[str, ...]:
        return registry.get(self.type).outputs

    def reference_names(self) -> set[str]:
        """Every valid way a condition may refer to this indicator."""
        outputs = self.outputs
        if not outputs:
            return {self.id}
        return {f"{self.id}.{o}" for o in outputs} | {self.id}


# ---------------------------------------------------------------------------
# Operands
# ---------------------------------------------------------------------------

class InlineIndicator(BaseModel):
    """An indicator declared inline inside a condition.

    Convenience for one-offs like "volume > SMA(volume, 20)" where naming the
    indicator separately would be noise.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    of: str = "close"

    @model_validator(mode="after")
    def _check_registry(self) -> InlineIndicator:
        try:
            registry.get(self.type)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @property
    def params(self) -> dict[str, Any]:
        reserved = {"type", "of"}
        return {k: v for k, v in self.model_dump().items() if k not in reserved}


class ExprFn(str, Enum):
    # Rolling: where the current value sits among the previous `window` values.
    RANK = "rank"            # percent of the previous `window` values strictly below, 0 to 100
    QUANTILE = "quantile"    # the q-quantile of the previous `window` values (linear)
    # Arithmetic on two operands, evaluated bar by bar.
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"


ROLLING_FNS = frozenset({ExprFn.RANK, ExprFn.QUANTILE})


class Expr(BaseModel):
    """A computed operand (schema v2): thresholds that are not fixed numbers.

    "RSI in the top 20% of its last 100 bars" is `{"fn": "rank", "of": "rsi",
    "window": 100}` compared `>` 80; "close above the 90th percentile of the
    last 50 highs" uses `quantile`; "close below VWAP minus 1.5 ATR" uses `sub`
    and `mul`.

    The rolling functions look only at the *previous* `window` bars, never the
    current one, so a value is never ranked against itself, and they are NaN
    until `window` valid values exist (so they never fire in warm-up). Their
    source must be a price field, a declared indicator or an inline indicator:
    session context (pivots, VWAP, PnL) is injected after indicators update, so
    a rolling window over it would see a different history in backtest and live.
    """

    model_config = ConfigDict(extra="forbid")

    fn: ExprFn
    of: Union[str, InlineIndicator, None] = None
    window: int | None = Field(default=None, ge=5, le=5000)
    q: float | None = Field(default=None, ge=0, le=1)
    args: list[Operand] | None = None

    @model_validator(mode="after")
    def _shape(self) -> Expr:
        if self.fn in ROLLING_FNS:
            if self.of is None or self.window is None:
                raise ValueError(f"{self.fn.value}: needs `of` and `window`")
            if self.args is not None:
                raise ValueError(f"{self.fn.value}: takes `of`, not `args`")
            if isinstance(self.of, str) and self.of in CONTEXT_FIELDS:
                raise ValueError(
                    f"{self.fn.value}: cannot roll over session context {self.of!r}; "
                    "use a price field or an indicator"
                )
            if self.fn is ExprFn.QUANTILE and self.q is None:
                raise ValueError("quantile: needs `q` between 0 and 1")
            if self.fn is ExprFn.RANK and self.q is not None:
                raise ValueError("rank: takes no `q`")
        else:
            if self.args is None or len(self.args) != 2:
                raise ValueError(f"{self.fn.value}: needs exactly two `args`")
            if self.of is not None or self.window is not None or self.q is not None:
                raise ValueError(f"{self.fn.value}: takes only `args`")
        return self

    @property
    def is_rolling(self) -> bool:
        return self.fn in ROLLING_FNS


Operand = Union[float, int, str, InlineIndicator, Expr]
Expr.model_rebuild()


def operand_refs(operand: Any) -> set[str]:
    """Every name an operand refers to, through any nesting of expressions."""
    if isinstance(operand, str):
        return {operand}
    if isinstance(operand, Expr):
        out: set[str] = set()
        for child in (operand.of, *(operand.args or ())):
            out |= operand_refs(child)
        return out
    return set()


def operand_exprs(operand: Any) -> list[Expr]:
    """Every expression inside an operand, innermost first."""
    if not isinstance(operand, Expr):
        return []
    out: list[Expr] = []
    for child in (operand.of, *(operand.args or ())):
        out.extend(operand_exprs(child))
    out.append(operand)
    return out


class Condition(BaseModel):
    """A single comparison: left <cmp> right."""

    model_config = ConfigDict(extra="forbid")

    left: Operand
    cmp: Comparator
    right: Operand

    def referenced_ids(self) -> set[str]:
        return operand_refs(self.left) | operand_refs(self.right)

    def exprs(self) -> list[Expr]:
        return operand_exprs(self.left) + operand_exprs(self.right)


class ConditionGroup(BaseModel):
    """A boolean tree. Nests arbitrarily so the UI can build compound logic."""

    model_config = ConfigDict(extra="forbid")

    op: BoolOp = BoolOp.AND
    conditions: list[Union[Condition, "ConditionGroup"]] = Field(min_length=1)

    def referenced_ids(self) -> set[str]:
        out: set[str] = set()
        for c in self.conditions:
            out |= c.referenced_ids()
        return out

    def exprs(self) -> list[Expr]:
        out: list[Expr] = []
        for c in self.conditions:
            out.extend(c.exprs())
        return out


ConditionGroup.model_rebuild()


# ---------------------------------------------------------------------------
# Sweeps (schema v2)
# ---------------------------------------------------------------------------

class SweepAxis(BaseModel):
    """One parameter to vary: a JSON Pointer to a number, and the values to try.

    The document itself always holds concrete values, so it runs as written;
    the sweep says which of those numbers a backtest grid should vary. Each
    variant is the document with the pointed-at numbers replaced and `sweep`
    removed, validated like any other document.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(pattern=r"^(/[^/]+)+$", max_length=200)
    values: list[int | float] = Field(min_length=2, max_length=MAX_SWEEP_VALUES)

    @model_validator(mode="after")
    def _distinct(self) -> SweepAxis:
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"sweep {self.path}: values repeat")
        if self.path.split("/")[1] in ("sweep", "schema_version", "id"):
            raise ValueError(f"sweep {self.path}: that field cannot be swept")
        return self


# ---------------------------------------------------------------------------
# Legs
# ---------------------------------------------------------------------------

class ExitRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: ExitType
    value: float = Field(gt=0)
    ref: str | None = None  # indicator id, required for atr_multiple

    @model_validator(mode="after")
    def _atr_needs_ref(self) -> ExitRule:
        if self.type is ExitType.ATR_MULTIPLE and not self.ref:
            raise ValueError("stop/target of type 'atr_multiple' requires `ref` naming an ATR indicator")
        return self


class TrailRule(ExitRule):
    """A trailing stop that only arms once the trade is meaningfully in profit."""

    activate_at: float = Field(default=0.0, ge=0)


STRIKE_RULE = re.compile(
    r"^(ATM|ITM|OTM)([+-]\d+)?$|^STRIKE:\d+(\.\d+)?$|^DELTA:0?\.\d+$", re.IGNORECASE
)


class OptionSpec(BaseModel):
    """How to pick the contract. Resolved to a real instrument at entry time."""

    model_config = ConfigDict(extra="forbid")

    right: OptionRight
    strike_rule: str = "ATM+0"
    expiry: Literal["current_weekly", "next_weekly", "current_monthly", "next_monthly"] = (
        "current_weekly"
    )

    @model_validator(mode="after")
    def _check_strike_rule(self) -> OptionSpec:
        if not STRIKE_RULE.match(self.strike_rule):
            raise ValueError(
                f"Invalid strike_rule {self.strike_rule!r}. Use ATM, ATM+2, OTM-1, "
                f"STRIKE:24800, or DELTA:0.30"
            )
        return self


class Leg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    action: Side
    kind: LegKind = LegKind.EQUITY
    lots: int = Field(default=1, ge=1, le=1000)
    option: OptionSpec | None = None

    stop_loss: ExitRule | None = None
    target: ExitRule | None = None
    trail: TrailRule | None = None

    @model_validator(mode="after")
    def _option_consistency(self) -> Leg:
        if self.kind is LegKind.OPTION and self.option is None:
            raise ValueError(f"Leg {self.id}: kind is 'option' but no `option` block given")
        if self.kind is not LegKind.OPTION and self.option is not None:
            raise ValueError(f"Leg {self.id}: `option` block given but kind is {self.kind.value!r}")
        return self


# ---------------------------------------------------------------------------
# Session, risk, costs
# ---------------------------------------------------------------------------

class SessionRules(BaseModel):
    """Intraday timing. `square_off` is a hard flatten, independent of signals."""

    model_config = ConfigDict(extra="forbid")

    start: time = time(9, 20)
    end: time = time(15, 10)
    square_off: time | None = time(15, 15)

    @model_validator(mode="after")
    def _ordering(self) -> SessionRules:
        if self.end <= self.start:
            raise ValueError("session.end must be after session.start")
        if self.square_off and self.square_off < self.end:
            raise ValueError("session.square_off must be at or after session.end")
        return self


class ReEntryRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max: int = Field(default=0, ge=0, le=50)
    cooldown_bars: int = Field(default=0, ge=0)


class RiskRules(BaseModel):
    """Strategy-level limits. The account-level kill switch sits above these and
    cannot be raised from here — see `RiskGuard`."""

    model_config = ConfigDict(extra="forbid")

    max_loss_per_day: float | None = Field(default=None, gt=0)
    max_profit_per_day: float | None = Field(default=None, gt=0)
    max_trades_per_day: int | None = Field(default=None, ge=1)
    max_concurrent_positions: int = Field(default=1, ge=1, le=50)
    re_entry: ReEntryRule = Field(default_factory=ReEntryRule)


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slippage_bps: float = Field(default=5.0, ge=0, le=500)
    brokerage_model: str = "upstox_fno"


class InstrumentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    segment: Literal["index", "equity", "future", "option", "commodity"] = "index"


def _pointer_tokens(path: str) -> list[str]:
    return [t.replace("~1", "/").replace("~0", "~") for t in path.split("/")[1:]]


def pointer_get(document: Any, path: str) -> Any:
    """The value at a JSON Pointer, or None when the path does not exist."""
    node = document
    for token in _pointer_tokens(path):
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def pointer_set(document: Any, path: str, value: Any) -> None:
    """Replace the value at an existing JSON Pointer, in place."""
    *parents, last = _pointer_tokens(path)
    node = document
    for token in parents:
        node = node[int(token)] if isinstance(node, list) else node[token]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------

class Strategy(BaseModel):
    """A complete, validated strategy definition."""

    model_config = ConfigDict(extra="forbid")

    # Omit it: the validator sets the lowest version that can express the document.
    schema_version: int | None = None
    id: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""

    instrument: InstrumentRef
    timeframe: str = "5m"
    session: SessionRules = Field(default_factory=SessionRules)

    indicators: list[IndicatorSpec] = Field(default_factory=list)
    entry: ConditionGroup
    exit: ConditionGroup | None = None
    legs: list[Leg] = Field(min_length=1, max_length=10)

    risk: RiskRules = Field(default_factory=RiskRules)
    costs: CostModel = Field(default_factory=CostModel)

    # Schema v2: numbers a backtest grid varies. Ignored by a single run.
    sweep: list[SweepAxis] = Field(default_factory=list, max_length=MAX_SWEEP_AXES)

    # -- validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _validate_document(self) -> Strategy:
        self._check_version()
        self._check_unique_ids()
        self._check_references()
        self._check_timeframe()
        self._check_exit_reachable()
        self._check_sweep()
        if not self.id:
            object.__setattr__(self, "id", f"stg_{self.content_hash()[:10]}")
        return self

    def v2_features(self) -> list[str]:
        """The version-2 features this document uses, for messages and versioning."""
        used = []
        exprs = self.entry.exprs() + (self.exit.exprs() if self.exit else [])
        if exprs:
            used.append("expression operands")
        if self.sweep:
            used.append("sweep")
        return used

    def _check_version(self) -> None:
        needed = 2 if self.v2_features() else 1
        if self.schema_version is None:
            object.__setattr__(self, "schema_version", needed)
            return
        if self.schema_version not in SUPPORTED_VERSIONS:
            raise ValueError(
                f"schema_version {self.schema_version} is not supported "
                f"(supported: {', '.join(map(str, SUPPORTED_VERSIONS))})"
            )
        if self.schema_version < needed:
            raise ValueError(
                f"schema_version {self.schema_version} cannot use "
                f"{' and '.join(self.v2_features())}; set schema_version to 2 or omit it"
            )

    def _check_sweep(self) -> None:
        if not self.sweep:
            return
        paths = [axis.path for axis in self.sweep]
        if len(set(paths)) != len(paths):
            raise ValueError("sweep: the same path appears twice")
        runs = 1
        for axis in self.sweep:
            runs *= len(axis.values)
        if runs > MAX_SWEEP_RUNS:
            raise ValueError(
                f"sweep: {runs} combinations is more than the {MAX_SWEEP_RUNS} allowed; "
                "use fewer values"
            )
        document = self.model_dump(mode="json", exclude={"sweep"})
        for axis in self.sweep:
            current = pointer_get(document, axis.path)
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                raise ValueError(
                    f"sweep {axis.path}: must point at a number written in the document "
                    f"(write defaults out explicitly to sweep them), found {current!r}"
                )

    def _check_unique_ids(self) -> None:
        ind_ids = [i.id for i in self.indicators]
        if len(ind_ids) != len(set(ind_ids)):
            dupes = {i for i in ind_ids if ind_ids.count(i) > 1}
            raise ValueError(f"Duplicate indicator id(s): {sorted(dupes)}")

        leg_ids = [leg.id for leg in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            dupes = {i for i in leg_ids if leg_ids.count(i) > 1}
            raise ValueError(f"Duplicate leg id(s): {sorted(dupes)}")

    def _known_references(self) -> set[str]:
        known = set(PRICE_FIELDS) | set(CONTEXT_FIELDS)
        for ind in self.indicators:
            known |= ind.reference_names()
        return known

    def _check_references(self) -> None:
        """Every string operand must resolve to something.

        This is the check that turns a typo into a load-time error rather than a
        strategy that silently never fires.
        """
        known = self._known_references()
        used: set[str] = set(self.entry.referenced_ids())
        if self.exit:
            used |= self.exit.referenced_ids()

        unknown = {u for u in used if u not in known}
        if unknown:
            raise ValueError(
                f"Condition references unknown name(s) {sorted(unknown)}. "
                f"Declare them in `indicators`, or use a price field "
                f"({', '.join(sorted(PRICE_FIELDS))})."
            )

        # ATR-based exits must point at a real ATR indicator.
        for leg in self.legs:
            for rule_name in ("stop_loss", "target", "trail"):
                rule = getattr(leg, rule_name)
                if rule and rule.ref:
                    match = next((i for i in self.indicators if i.id == rule.ref), None)
                    if match is None:
                        raise ValueError(
                            f"Leg {leg.id}.{rule_name} references unknown indicator {rule.ref!r}"
                        )
                    if match.type.upper() != "ATR":
                        raise ValueError(
                            f"Leg {leg.id}.{rule_name} ref {rule.ref!r} must be an ATR "
                            f"indicator, got {match.type}"
                        )

    def _check_timeframe(self) -> None:
        from backend.data.upstox_client import Timeframe

        try:
            Timeframe.parse(self.timeframe)
        except ValueError as exc:
            raise ValueError(f"Invalid timeframe {self.timeframe!r}: {exc}") from exc

    def _check_exit_reachable(self) -> None:
        """A strategy with no way out is almost always a mistake.

        Square-off counts as an exit, so intraday strategies relying on it alone
        are fine — but a positional strategy with no exit conditions and no
        per-leg stops would hold forever.
        """
        has_exit_conditions = self.exit is not None
        has_leg_exits = any(leg.stop_loss or leg.target or leg.trail for leg in self.legs)
        has_square_off = self.session.square_off is not None

        if not (has_exit_conditions or has_leg_exits or has_square_off):
            raise ValueError(
                "Strategy has no exit path: define `exit` conditions, per-leg "
                "stop_loss/target, or a session.square_off time."
            )

    # -- identity -----------------------------------------------------------

    def content_hash(self) -> str:
        """Stable hash of everything that affects behaviour.

        `id`, `name` and `description` are excluded so renaming a strategy does
        not orphan its historical results.
        """
        payload = self.model_dump(mode="json", exclude={"id", "name", "description"})
        # Fields added in v2 are left out while empty, so a v1 document hashes
        # exactly as it did before they existed.
        if not payload.get("sweep"):
            payload.pop("sweep", None)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    # -- serialization ------------------------------------------------------

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=indent, default=str)

    @classmethod
    def from_json(cls, text: str | bytes) -> Strategy:
        return cls.model_validate(json.loads(text))

    @classmethod
    def from_file(cls, path) -> Strategy:
        from pathlib import Path

        return cls.from_json(Path(path).read_text())

    def save(self, path) -> None:
        from pathlib import Path

        Path(path).write_text(self.to_json())

    # -- introspection ------------------------------------------------------

    @property
    def is_intraday(self) -> bool:
        return self.session.square_off is not None

    @property
    def has_options(self) -> bool:
        return any(leg.kind is LegKind.OPTION for leg in self.legs)

    def indicator_by_id(self, ind_id: str) -> IndicatorSpec | None:
        return next((i for i in self.indicators if i.id == ind_id), None)

    def all_exprs(self) -> list[Expr]:
        return self.entry.exprs() + (self.exit.exprs() if self.exit else [])

    def rolling_lookback(self) -> int:
        """Bars of history the longest rolling expression needs beyond its source."""
        return max((e.window or 0 for e in self.all_exprs() if e.is_rolling), default=0)

    def summary(self) -> str:
        legs = ", ".join(
            f"{leg.action.value} {leg.lots}x "
            f"{leg.option.right.value + ' ' + leg.option.strike_rule if leg.option else leg.kind.value}"
            for leg in self.legs
        )
        return f"{self.name} [{self.instrument.symbol} {self.timeframe}] -> {legs}"
