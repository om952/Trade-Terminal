"""Schema v2 (P2.2): expression thresholds, sweeps, and v1 compatibility.

The compatibility tests are the gate: every v1 document must keep its exact
content hash, id and family, because stored runs, journals and trial counts are
keyed by them. The parity tests extend the Phase 2 guarantee (vectorized and
streaming agree to 1e-9) to the new expression types.
"""

from __future__ import annotations

import json
import math
from collections import deque
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from backend.agent import readback
from backend.data.simulator import generate_ohlcv
from backend.evals import canonical
from backend.indicators.base import bars_from_frame
from backend.strategy import sweep, templates
from backend.strategy.engine import StrategyEngine
from backend.strategy.family import family_key
from backend.strategy.indicator_set import (
    StreamingIndicatorSet,
    VectorizedIndicatorSet,
    expr_key,
    rolling_series,
    rolling_value,
)
from backend.strategy.runner import run_backtest
from backend.strategy.schema import (
    MAX_SWEEP_RUNS,
    Expr,
    Strategy,
)

GOLDEN = json.loads((Path(__file__).parent / "data" / "v1_golden.json").read_text())
SYMBOL = "SIM|V2TEST"


def base_doc(**overrides) -> dict:
    doc = templates.load("ema_crossover", symbol=SYMBOL).model_dump(
        mode="json", exclude={"id", "schema_version"}
    )
    doc.update(overrides)
    return doc


def v2_doc() -> dict:
    """An EMA crossover gated on ATR rank, with expression-valued exits."""
    d = base_doc()
    d["indicators"].append({"id": "rsi", "type": "RSI", "params": {"period": 14}})
    d["entry"]["conditions"].append(
        {"left": {"fn": "rank", "of": "atr", "window": 50}, "cmp": ">", "right": 40}
    )
    d["exit"]["conditions"].append({
        "left": "close", "cmp": "crosses_below",
        "right": {"fn": "sub", "args": ["ema_slow", {"fn": "mul", "args": ["atr", 0.5]}]},
    })
    d["exit"]["conditions"].append({
        "left": "rsi", "cmp": ">",
        "right": {"fn": "quantile", "of": "rsi", "window": 100, "q": 0.95},
    })
    return d


@pytest.fixture(scope="module")
def frame():
    return generate_ohlcv("RELIANCE", date(2026, 1, 1), date(2026, 3, 31), "5m", seed=909)


# ---------------------------------------------------------------------------
# v1 compatibility (the gate)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_v1_documents_keep_hash_id_and_family(key):
    golden = GOLDEN[key]
    stored = Strategy.model_validate(golden["document"])
    assert stored.schema_version == 1
    assert stored.content_hash() == golden["hash"]
    assert stored.id == golden["document"]["id"]
    assert family_key(stored) == golden["family"]

    # The model is told to omit schema_version; such a document is still v1.
    bare = {k: v for k, v in golden["document"].items() if k not in ("schema_version", "id")}
    fresh = Strategy.model_validate(bare)
    assert fresh.schema_version == 1
    assert fresh.content_hash() == golden["hash"]
    assert fresh.id == golden["document"]["id"]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_templates_still_produce_v1(key):
    assert templates.load(key).content_hash() == GOLDEN[key]["hash"]


def test_v2_features_set_the_version():
    assert Strategy.model_validate(v2_doc()).schema_version == 2
    swept = base_doc(sweep=[{"path": "/indicators/0/params/period", "values": [5, 9]}])
    assert Strategy.model_validate(swept).schema_version == 2


def test_v1_claim_with_v2_features_is_refused():
    with pytest.raises(ValidationError, match="schema_version 1 cannot use expression operands"):
        Strategy.model_validate({**v2_doc(), "schema_version": 1})


def test_unknown_version_is_refused():
    with pytest.raises(ValidationError, match="schema_version 3 is not supported"):
        Strategy.model_validate({**base_doc(), "schema_version": 3})


def test_explicit_v2_without_features_is_allowed():
    assert Strategy.model_validate({**base_doc(), "schema_version": 2}).schema_version == 2


def test_v2_roundtrip_preserves_hash():
    s = Strategy.model_validate(v2_doc())
    assert Strategy.from_json(s.to_json()).content_hash() == s.content_hash()


# ---------------------------------------------------------------------------
# Expression validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operand, message", [
    ({"fn": "rank", "of": "rsi"}, "needs `of` and `window`"),
    ({"fn": "rank", "of": "rsi", "window": 2}, "greater than or equal to 5"),
    ({"fn": "quantile", "of": "rsi", "window": 20}, "needs `q`"),
    ({"fn": "rank", "of": "rsi", "window": 20, "q": 0.5}, "takes no `q`"),
    ({"fn": "rank", "of": "vwap", "window": 20}, "cannot roll over session context"),
    ({"fn": "add", "args": ["close"]}, "exactly two `args`"),
    ({"fn": "mul", "args": ["close", 2], "window": 5}, "takes only `args`"),
    ({"fn": "pow", "args": ["close", 2]}, "fn"),
])
def test_bad_expressions_are_refused(operand, message):
    d = base_doc()
    d["indicators"].append({"id": "rsi", "type": "RSI", "params": {"period": 14}})
    d["entry"]["conditions"].append({"left": operand, "cmp": ">", "right": 1})
    with pytest.raises(ValidationError, match=message):
        Strategy.model_validate(d)


def test_names_inside_expressions_are_checked():
    d = base_doc()
    d["entry"]["conditions"].append({
        "left": "close", "cmp": ">",
        "right": {"fn": "add", "args": [{"fn": "rank", "of": "ema_fats", "window": 10}, 1]},
    })
    with pytest.raises(ValidationError, match="ema_fats"):
        Strategy.model_validate(d)


# ---------------------------------------------------------------------------
# Rolling computation
# ---------------------------------------------------------------------------

def _naive(values: list[float], fn: str, window: int, q: float | None = None) -> list[float]:
    out = []
    for i, x in enumerate(values):
        prev = values[i - window:i] if i >= window else []
        if len(prev) < window or any(math.isnan(v) for v in prev):
            out.append(math.nan)
        elif fn == "rank":
            out.append(math.nan if math.isnan(x) else 100.0 * sum(v < x for v in prev) / window)
        else:
            out.append(float(np.quantile(prev, q)))
    return out


@pytest.mark.parametrize("fn, q", [("rank", None), ("quantile", 0.2), ("quantile", 0.9)])
def test_rolling_matches_a_hand_calculation(fn, q):
    rng = np.random.default_rng(4)
    values = rng.normal(size=300).round(1)        # rounding forces ties
    values[[10, 150]] = np.nan                    # gaps poison their windows
    expr = Expr(fn=fn, of="close", window=20, q=q)
    got = rolling_series(values, expr)
    want = _naive(list(values), fn, 20, q)
    for i, (a, b) in enumerate(zip(got, want)):
        assert (math.isnan(a) and math.isnan(b)) or abs(a - b) < 1e-12, i


def test_rolling_never_sees_the_current_bar():
    """A spike is ranked against what came before it, so it ranks 100."""
    values = np.array([1.0] * 10 + [50.0])
    assert rolling_series(values, Expr(fn="rank", of="close", window=10))[-1] == 100.0


def test_rolling_is_chunked_for_long_windows():
    rng = np.random.default_rng(1)
    values = rng.normal(size=3000)
    expr = Expr(fn="quantile", of="close", window=2000, q=0.5)
    history: deque[float] = deque(maxlen=2000)
    last = math.nan
    for v in values:
        last = rolling_value(history, float(v), expr)
        history.append(float(v))
    assert abs(rolling_series(values, expr)[-1] - last) < 1e-12


def _conditions_doc() -> Strategy:
    d = v2_doc()
    # An inline indicator inside an expression, and arithmetic over context.
    d["entry"]["conditions"].append({
        "left": {"fn": "rank", "of": {"type": "SMA", "of": "volume", "period": 10}, "window": 30},
        "cmp": ">=", "right": 0,
    })
    d["entry"]["conditions"].append({
        "left": "close", "cmp": ">", "right": {"fn": "div", "args": ["prev_day_low", 2]},
    })
    return Strategy.model_validate(d)


def test_vectorized_and_streaming_agree(frame):
    """The Phase 2 guarantee, extended to expressions."""
    strategy = _conditions_doc()
    engine = StrategyEngine(strategy)
    vec = VectorizedIndicatorSet(frame, strategy.indicators, engine._inline, engine._exprs)
    stream = StreamingIndicatorSet(strategy.indicators, engine._inline, engine._exprs)
    rolling = [e for e in strategy.all_exprs() if e.is_rolling]
    assert len(rolling) == 3
    keys = [expr_key(e) for e in rolling]

    compared = 0
    for i, bar in enumerate(bars_from_frame(frame)):
        vec.seek(i)
        stream.update(bar)
        for key in keys:
            a, b = vec.value(key), stream.value(key)
            assert (a != a and b != b) or abs(a - b) < 1e-9, f"{key} bar {i}: {a} vs {b}"
            compared += a == a
            pa, pb = vec.previous(key), stream.previous(key)
            assert (pa != pa and pb != pb) or abs(pa - pb) < 1e-9
    assert compared > 3 * 4000


def test_rolling_expressions_extend_warmup(frame):
    strategy = Strategy.model_validate(v2_doc())
    engine = StrategyEngine(strategy)
    indicators = engine.prepare_backtest(frame)
    # quantile over RSI(14) needs 100 earlier RSI values.
    assert engine._warmup >= 14 + 100


def test_expression_strategy_trades_and_uses_its_exits(frame):
    strategy = Strategy.model_validate(v2_doc())
    result = run_backtest(strategy, frame, starting_capital=Decimal(1_000_000),
                          lot_size=250, collect_events=False)
    reasons = {t.reason.value for t in result.trades}
    assert len(result.trades) > 20
    assert "exit_signal" in reasons


def test_division_by_zero_is_never_true():
    from backend.strategy.conditions import _arithmetic
    from backend.strategy.schema import ExprFn

    assert math.isnan(_arithmetic(ExprFn.DIV, 1.0, 0.0))
    assert math.isnan(_arithmetic(ExprFn.ADD, math.nan, 1.0))


# ---------------------------------------------------------------------------
# Read-back, family, eval canonical form
# ---------------------------------------------------------------------------

def test_readback_spells_out_expressions():
    card = readback.as_text(readback.render(Strategy.model_validate(v2_doc())))
    assert "the percentile rank (0 to 100) of atr among the previous 50 bars > 40" in card
    assert "(ema_slow - (atr x 0.5))" in card
    assert "the 95th percentile of rsi over the previous 100 bars" in card


def test_family_ignores_windows_but_not_functions():
    a = Strategy.model_validate(v2_doc())
    longer = v2_doc()
    longer["entry"]["conditions"][1]["left"]["window"] = 80
    assert family_key(a) == family_key(Strategy.model_validate(longer))

    other = v2_doc()
    other["entry"]["conditions"][1]["left"] = {
        "fn": "quantile", "of": "atr", "window": 50, "q": 0.4,
    }
    assert family_key(a) != family_key(Strategy.model_validate(other))


def test_family_treats_commutative_arithmetic_as_one():
    a, b = v2_doc(), v2_doc()
    b["exit"]["conditions"][1]["right"]["args"][1]["args"] = [0.5, "atr"]
    assert family_key(Strategy.model_validate(a)) == family_key(Strategy.model_validate(b))


def test_canonical_form_keeps_windows():
    s = Strategy.model_validate(v2_doc())
    text = canonical.operand(s, s.entry.conditions[1].left)
    assert text.startswith("rank(ATR(") and "window=50" in text


# ---------------------------------------------------------------------------
# Sweeps
# ---------------------------------------------------------------------------

def swept(*axes) -> dict:
    return base_doc(sweep=[{"path": p, "values": v} for p, v in axes])


def test_sweep_expands_every_combination():
    s = Strategy.model_validate(swept(
        ("/indicators/0/params/period", [5, 9, 13]),
        ("/legs/0/stop_loss/value", [1.0, 2.5]),
    ))
    grid = sweep.variants(s)
    assert len(grid) == 6 == sweep.count(s)
    assert all(v.strategy is not None for v in grid)
    periods = {v.strategy.indicators[0].params["period"] for v in grid}
    assert periods == {5, 9, 13}
    assert all(isinstance(v.strategy.indicators[0].params["period"], int) for v in grid)
    assert grid[0].label == "ema_fast.period=5, leg1.stop_loss.value=1"
    # Variants are plain documents: no sweep, back to v1, one family.
    assert all(not v.strategy.sweep and v.strategy.schema_version == 1 for v in grid)
    assert len({family_key(v.strategy) for v in grid}) == 1
    assert family_key(grid[0].strategy) == family_key(s)
    assert len({v.strategy.content_hash() for v in grid}) == 6


def test_a_variant_equal_to_the_plain_document_shares_its_hash():
    plain = Strategy.model_validate(base_doc())
    s = Strategy.model_validate(swept(("/indicators/0/params/period", [9, 12])))
    assert sweep.variants(s)[0].strategy.content_hash() == plain.content_hash()


def test_invalid_variants_are_skipped_with_a_reason():
    s = Strategy.model_validate(swept(("/legs/0/lots", [1, 0])))
    grid = sweep.variants(s)
    assert grid[0].strategy is not None
    assert grid[1].strategy is None and "lots" in grid[1].error


@pytest.mark.parametrize("axes, message", [
    ([("/indicators/0/params/period", [5])], "at least 2"),
    ([("/indicators/0/params/period", [5, 5])], "values repeat"),
    ([("/name", [1, 2])], "must point at a number"),
    ([("/indicators/9/params/period", [1, 2])], "must point at a number"),
    ([("/indicators/2/params/source", [1, 2])], "must point at a number"),
    ([("/schema_version", [1, 2])], "cannot be swept"),
    ([("/indicators/0/params/period", [5, 9]), ("/indicators/0/params/period", [6, 7])],
     "same path appears twice"),
    ([("/indicators/0/params/period", list(range(2, 52))),
      ("/indicators/1/params/period", list(range(2, 7)))],
     f"more than the {MAX_SWEEP_RUNS} allowed"),
])
def test_bad_sweeps_are_refused(axes, message):
    with pytest.raises(ValidationError, match=message):
        Strategy.model_validate(swept(*axes))


def test_sweep_changes_the_hash_but_not_a_single_run(frame):
    plain = Strategy.model_validate(base_doc())
    s = Strategy.model_validate(swept(("/indicators/0/params/period", [9, 12])))
    assert s.content_hash() != plain.content_hash()
    kwargs = dict(starting_capital=Decimal(1_000_000), lot_size=250, collect_events=False)
    a, b = run_backtest(plain, frame, **kwargs), run_backtest(s, frame, **kwargs)
    assert [t.as_dict() | {"strategy_id": None} for t in a.trades] == \
        [t.as_dict() | {"strategy_id": None} for t in b.trades]


def test_readback_lists_the_sweep():
    s = Strategy.model_validate(swept(
        ("/indicators/0/params/period", [5, 9]), ("/legs/0/lots", [1, 2, 3]),
    ))
    text = readback.as_text(readback.render(s))
    assert "ema_fast.period: 5, 9" in text
    assert "leg1.lots: 1, 2, 3" in text
    assert "6 combinations" in text


def test_prompt_documents_v2():
    from backend.agent import prompts

    prompts.builder_system.cache_clear()
    text = prompts.builder_system()
    assert '"fn": "rank"' in text and "sweep" in text
    assert "Leave schema_version out" in text
