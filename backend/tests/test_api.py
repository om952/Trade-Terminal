"""Phase 5 gate: the API surface, and proof the UI path produces identical results.

The gate test is `test_gate_ui_path_matches_direct_engine` — a strategy sent
through the HTTP endpoint must produce exactly the same trades and metrics as
calling the engine directly. If those ever diverge, the API has grown logic of
its own and the one-engine guarantee is broken.
"""

from __future__ import annotations

import warnings
from datetime import date
from decimal import Decimal

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient

from backend.analytics import metrics as M
from backend.api.main import app
from backend.data.simulator import generate_ohlcv
from backend.data.store import write_candles
from backend.execution.costs import segment_for
from backend.strategy import templates
from backend.strategy.risk import RiskGuard
from backend.strategy.runner import FillModel, run_backtest
from backend.strategy.schema import Strategy


@pytest.fixture(scope="module")
def client() -> TestClient:
    # The API only answers requests addressed to this machine.
    return TestClient(app, base_url="http://localhost")


@pytest.fixture(scope="module")
def seeded_dataset() -> str:
    """A deterministic dataset the API can back-test against.

    Seeded at every timeframe the shipped templates use, so the gate can run all
    twelve of them without a missing-data 404 masking a real divergence.
    """
    key = "SIM|APITEST"
    for timeframe in ("5m", "15m", "1d"):
        df = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2026, 6, 30), timeframe, seed=4242)
        write_candles(key, timeframe, df, source="simulated")
    return key


# ---------------------------------------------------------------------------
# Reference endpoints
# ---------------------------------------------------------------------------

def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_status_shape(client):
    body = client.get("/api/status").json()
    assert set(body) >= {"auth", "instruments", "datasets", "mode", "risk_configured"}
    assert isinstance(body["auth"]["valid"], bool)


def test_indicator_palette_matches_registry(client):
    from backend.indicators import registry

    body = client.get("/api/indicators").json()
    assert len(body["indicators"]) == len(registry.all_names())
    assert set(body["categories"]) == {"trend", "momentum", "volatility", "volume"}
    for entry in body["indicators"]:
        assert entry["outputs"], f"{entry['name']} declares no outputs"


def test_template_catalogue(client):
    body = client.get("/api/templates").json()
    assert len(body["templates"]) == len(templates.TEMPLATES)
    assert {t["category"] for t in body["templates"]} == {"directional", "options"}


@pytest.mark.parametrize("key", sorted(templates.TEMPLATES))
def test_every_template_loads_and_revalidates(client, key):
    """A template fetched over HTTP must still validate when posted back."""
    document = client.get(f"/api/templates/{key}").json()
    response = client.post("/api/strategies/validate", json=document)
    assert response.status_code == 200
    assert response.json()["valid"], response.json()


def test_unknown_template_404s(client):
    assert client.get("/api/templates/does_not_exist").status_code == 404


def test_instrument_search_ranks_exact_match_first(client, instrument_master):
    results = client.get("/api/instruments/search", params={"q": "RELIANCE"}).json()["results"]
    assert results, "instrument master not synced"
    assert results[0]["trading_symbol"] == "RELIANCE"
    assert results[0]["exchange"] == "NSE"
    assert results[0]["instrument_type"] == "EQ"


def test_datasets_lists_stored_data(client, seeded_dataset):
    body = client.get("/api/datasets").json()
    keys = {d["instrument_key"] for d in body["datasets"]}
    assert seeded_dataset in keys
    entry = next(d for d in body["datasets"] if d["instrument_key"] == seeded_dataset)
    assert entry["synthetic"] is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_returns_hash_for_good_document(client):
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/strategies/validate", json=document).json()
    assert body["valid"]
    assert len(body["hash"]) == 64
    assert body["id"].startswith("stg_")


def test_validate_reports_field_and_message(client):
    body = client.post("/api/strategies/validate", json={
        "name": "x", "instrument": {"symbol": "S"},
        "indicators": [{"id": "ema", "type": "EMA"}],
        "entry": {"conditions": [{"left": "typo_here", "cmp": ">", "right": 1}]},
        "legs": [{"id": "l", "action": "BUY"}],
    }).json()

    assert body["valid"] is False
    assert any("typo_here" in e["message"] for e in body["errors"])


def test_validate_never_raises_on_garbage(client):
    """The builder posts on every keystroke; malformed drafts must not 500."""
    for payload in ({}, {"name": "x"}, {"legs": []}, {"entry": None}):
        response = client.post("/api/strategies/validate", json=payload)
        assert response.status_code == 200
        assert response.json()["valid"] is False


# ---------------------------------------------------------------------------
# Backtest endpoint
# ---------------------------------------------------------------------------

def test_backtest_returns_full_payload(client, seeded_dataset):
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250, "include_chart": True,
    }).json()

    assert set(body) >= {
        "metrics", "trades", "equity_curve", "costs", "chart",
        "warnings", "blocked_reasons", "strategy_hash",
    }
    assert body["synthetic_data"] is True
    assert body["chart"]["candles"]
    assert body["metrics"]["total_trades"] == len(body["trades"])


def test_backtest_chart_separates_overlays_from_oscillators(client, seeded_dataset):
    """RSI on the price scale would flatten the candles into a line."""
    document = client.get("/api/templates/rsi_mean_reversion").json()
    document["timeframe"] = "5m"
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250,
    }).json()

    pane_types = {p["type"] for p in body["chart"]["panes"]}
    overlay_types = {o["type"] for o in body["chart"]["overlays"]}
    assert "RSI" in pane_types
    assert "RSI" not in overlay_types


def test_backtest_markers_pair_with_trades(client, seeded_dataset):
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250,
    }).json()

    # Two markers per trade (entry + exit), minus any entry scrolled off the
    # truncated chart window.
    assert len(body["chart"]["markers"]) <= 2 * len(body["trades"])
    assert body["chart"]["markers"] == sorted(body["chart"]["markers"], key=lambda m: m["time"])


def test_backtest_rejects_invalid_strategy(client, seeded_dataset):
    response = client.post("/api/backtest", json={
        "strategy": {"name": "broken"}, "symbol": seeded_dataset,
        "starting_capital": 100000, "lot_size": 1,
    })
    assert response.status_code == 422


def test_backtest_404s_on_missing_data(client):
    document = client.get("/api/templates/ema_crossover").json()
    response = client.post("/api/backtest", json={
        "strategy": document, "symbol": "SIM|NOSUCHTHING",
        "starting_capital": 100000, "lot_size": 1,
    })
    assert response.status_code == 404
    assert "No stored candles" in response.json()["detail"]


def test_backtest_surfaces_blocked_reasons(client, seeded_dataset):
    """An undercapitalized run must explain itself rather than silently do nothing."""
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 20_000, "lot_size": 250, "max_leverage": 5,
    }).json()

    assert body["metrics"]["total_trades"] == 0
    assert "insufficient_capital" in body["blocked_reasons"]


def test_backtest_reports_cost_context(client, seeded_dataset):
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250,
    }).json()

    costs = body["costs"]
    assert costs["breakeven_move"] > 0
    assert costs["round_trip"]["total"] > 0
    assert costs["round_trip"]["stt"] >= 0


def test_equity_curve_is_downsampled_for_the_browser(client, seeded_dataset):
    document = client.get("/api/templates/ema_crossover").json()
    body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250,
    }).json()
    assert len(body["equity_curve"]) <= 4000
    assert all("drawdown" in p for p in body["equity_curve"])


def test_cost_preview_endpoint(client):
    body = client.get("/api/costs/preview", params={
        "segment": "futures", "quantity": 75, "price": 24800,
    }).json()
    assert body["round_trip"]["stt"] == pytest.approx(930.0)  # 0.05% sell-side, 2026 rate
    assert body["breakeven_move"] > 0


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(templates.TEMPLATES))
def test_gate_ui_path_matches_direct_engine(client, seeded_dataset, key):
    """Every template, run through HTTP, must equal the direct engine call.

    This is the Phase 5 gate: the UI is a view over the engine, not a second
    implementation of it. Any divergence means the API layer has grown trading
    logic and results shown on screen can no longer be trusted to match what a
    paper or live session would do.
    """
    document = client.get(f"/api/templates/{key}").json()
    capital, lot_size = 1_000_000, 250

    api_body = client.post("/api/backtest", json={
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": capital, "lot_size": lot_size,
        "slippage_bps": 5, "max_leverage": 5, "include_chart": False,
    })
    assert api_body.status_code == 200, api_body.text
    api_result = api_body.json()

    # --- the same run, straight through the engine -------------------------
    from backend.data.store import read_candles

    strategy = Strategy.model_validate(document)
    df = read_candles(seeded_dataset, strategy.timeframe)
    segment = segment_for(
        strategy.instrument.segment, strategy.is_intraday, strategy.legs[0].kind.value
    )
    direct = run_backtest(
        strategy, df,
        starting_capital=Decimal(str(capital)),
        lot_size=lot_size,
        fill_model=FillModel(slippage_bps=5, segment=segment),
        risk=RiskGuard(rules=strategy.risk, max_leverage=Decimal("5")),
        collect_events=False,
    )
    direct_metrics = M.compute_metrics(
        direct.equity_curve, direct.trades, Decimal(str(capital)), strategy.timeframe
    )

    assert api_result["strategy_hash"] == strategy.content_hash()
    assert api_result["metrics"]["total_trades"] == direct_metrics.total_trades
    assert api_result["metrics"]["net_pnl"] == pytest.approx(direct_metrics.net_pnl, abs=0.01)
    assert api_result["metrics"]["total_fees"] == pytest.approx(direct_metrics.total_fees, abs=0.01)
    assert api_result["metrics"]["win_rate_pct"] == pytest.approx(direct_metrics.win_rate_pct, abs=1e-6)

    for over_http, engine_trade in zip(api_result["trades"], direct.trades):
        assert over_http["entry_time"] == engine_trade.entry_time.isoformat()
        assert over_http["net_pnl"] == pytest.approx(float(engine_trade.net_pnl), abs=0.01)
        assert over_http["reason"] == engine_trade.reason.value


def test_gate_repeated_runs_are_deterministic(client, seeded_dataset):
    """The same request twice must give byte-identical results."""
    document = client.get("/api/templates/bollinger_breakout").json()
    payload = {
        "strategy": document, "symbol": seeded_dataset,
        "starting_capital": 1_000_000, "lot_size": 250, "include_chart": False,
    }
    first = client.post("/api/backtest", json=payload).json()
    second = client.post("/api/backtest", json=payload).json()

    assert first["metrics"] == second["metrics"]
    assert first["trades"] == second["trades"]


# ---------------------------------------------------------------------------
# Live / paper sessions over HTTP
# ---------------------------------------------------------------------------

def _await_session(client, session_id, timeout=60.0):
    import time as _t

    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        snap = client.get(f"/api/live/{session_id}", params={"include_bars": False}).json()
        if not snap.get("running"):
            return snap
        _t.sleep(0.2)
    raise AssertionError(f"session {session_id} did not finish in {timeout}s")


def test_gate_replay_session_matches_backtest_given_the_same_history(client, seeded_dataset):
    """A paper session replaying a window must equal a backtest that saw the same
    history — which means the backtest has to start earlier, not at the window edge.

    Two things are pinned here.

    The bug: warm-up used to come from the newest bars in the store regardless of
    where the replay started, so replaying an old window primed indicators with
    values from months later. The session then disagreed with its own backtest
    over the opening bars and quietly converged, leaving a one-trade difference
    with nothing to point at.

    The subtlety: a backtest run over exactly `[start, end]` cold-starts its
    indicators at `start`, while a live session warms up on the bars *before* it.
    Wilder smoothing has infinite memory, so a cold ATR is measurably different
    for the first bars — enough to move an ATR-derived stop by several rupees on
    the opening trade. That is not a defect in either path; it is the two paths
    being given different information. Comparing them fairly means handing the
    backtest the same prior bars and then comparing only the overlapping window.
    """
    document = client.get("/api/templates/ema_crossover").json()
    document["timeframe"] = "5m"
    start, end = "2025-09-01", "2026-06-30"
    common = {"symbol": seeded_dataset, "starting_capital": 1_000_000, "lot_size": 250}

    started = client.post("/api/live/start", json={
        "strategy": document, **common, "source": "replay",
        "replay_start": start, "replay_end": end,
        "replay_speed": 0, "alerts": False,
    })
    assert started.status_code == 200, started.text
    snapshot = _await_session(client, started.json()["session_id"])

    # Backtest from the beginning of stored history so its indicators are just as
    # warm as the session's by the time the window opens.
    backtest = client.post("/api/backtest", json={
        "strategy": document, **common, "end": end, "include_chart": False,
    }).json()
    overlapping = [t for t in backtest["trades"] if t["entry_time"][:10] >= start]

    assert snapshot["state"] == "stopped"
    assert snapshot["reconciled"], snapshot["errors"]
    assert snapshot["trades"], "no trades — the comparison would be vacuous"
    assert len(snapshot["trades"]) == len(overlapping)

    for paper, back in zip(snapshot["trades"], overlapping):
        assert paper["entry_time"] == back["entry_time"]
        assert paper["exit_time"] == back["exit_time"]
        assert paper["reason"] == back["reason"]
        assert paper["entry_price"] == pytest.approx(back["entry_price"], abs=1e-6)
        assert paper["exit_price"] == pytest.approx(back["exit_price"], abs=1e-6)
        assert paper["net_pnl"] == pytest.approx(back["net_pnl"], abs=0.01)


def test_replay_warmup_is_drawn_from_before_the_window(client, seeded_dataset):
    """The invariant behind the gate above, asserted directly.

    Warm-up bars must be the ones immediately preceding the replay start. Taking
    them from the end of the store instead is silent: indicators simply hold
    values from the wrong period and converge away from the evidence.
    """
    from backend.live.manager import manager

    document = client.get("/api/templates/ema_crossover").json()
    document["timeframe"] = "5m"
    start = "2026-01-05"

    started = client.post("/api/live/start", json={
        "strategy": document, "symbol": seeded_dataset, "source": "replay",
        "replay_start": start, "replay_end": "2026-02-28",
        "replay_speed": 0, "alerts": False,
        "starting_capital": 1_000_000, "lot_size": 250,
    }).json()
    session_id = started["session_id"]
    _await_session(client, session_id)

    session = manager.get(session_id)
    warmup = session.feed.warmup_frame
    assert warmup is not None and not warmup.empty
    assert str(warmup["timestamp"].iloc[-1].date()) < start, (
        "warm-up ran past the replay start — indicators saw bars they should not have"
    )
    assert str(warmup["timestamp"].iloc[0].date()) < start


def test_live_start_refuses_a_replay_window_with_no_data(client, seeded_dataset):
    document = client.get("/api/templates/ema_crossover").json()
    document["timeframe"] = "5m"
    response = client.post("/api/live/start", json={
        "strategy": document, "symbol": seeded_dataset, "source": "replay",
        "replay_start": "2030-01-01", "replay_end": "2030-02-01", "alerts": False,
    })
    assert response.status_code == 404


def test_live_sessions_lists_active_and_past(client):
    body = client.get("/api/live/sessions").json()
    assert set(body) == {"active", "history"}
    assert isinstance(body["active"], list)


def test_live_snapshot_falls_back_to_the_journal(client, seeded_dataset):
    """A session this process never ran must still be readable off disk."""
    document = client.get("/api/templates/ema_crossover").json()
    document["timeframe"] = "5m"
    started = client.post("/api/live/start", json={
        "strategy": document, "symbol": seeded_dataset, "source": "replay",
        "replay_start": "2026-05-01", "replay_end": "2026-06-30",
        "replay_speed": 0, "alerts": False, "starting_capital": 1_000_000, "lot_size": 250,
    }).json()
    session_id = started["session_id"]
    _await_session(client, session_id)

    from backend.live.manager import manager

    manager._sessions.pop(session_id, None)      # as if the API had restarted
    manager._threads.pop(session_id, None)

    recovered = client.get(f"/api/live/{session_id}").json()
    assert recovered["from_journal"] is True
    assert recovered["running"] is False
    assert recovered["bars_seen"] > 0


def test_unknown_live_session_404s(client):
    assert client.get("/api/live/no-such-session").status_code == 404


def test_api_shutdown_stops_sessions_resumably(seeded_dataset):
    """Stopping the server must end running sessions as 'shutdown', so a restart
    can offer to resume them, instead of abandoning them mid-write."""
    import logging
    import time as _time

    from backend.core import log_setup
    from backend.live import journal as J

    document = TestClient(app, base_url="http://localhost").get(
        "/api/templates/ema_crossover"
    ).json()
    document["timeframe"] = "5m"
    document["name"] = "shutdown hook test"  # own hash, so its own journal

    try:
        # Entering the client runs startup; leaving it runs shutdown.
        with TestClient(app, base_url="http://localhost") as lifespan_client:
            started = lifespan_client.post("/api/live/start", json={
                "strategy": document, "symbol": seeded_dataset, "source": "replay",
                "replay_start": "2026-01-05", "replay_end": "2026-02-28",
                "replay_speed": 600, "alerts": False,
                "starting_capital": 1_000_000, "lot_size": 250,
            }).json()
            session_id = started["session_id"]
            deadline = _time.monotonic() + 30
            while _time.monotonic() < deadline:
                snap = lifespan_client.get(
                    f"/api/live/{session_id}", params={"include_bars": False}
                ).json()
                if snap.get("bars_seen", 0) >= 3:
                    break
                _time.sleep(0.1)
            assert snap["running"], "session never got going"

        state = J.recover(session_id)
        assert state.ended and state.end_reason == "shutdown"
        assert state.resumable
    finally:
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, log_setup._OurHandler):
                root.removeHandler(handler)
                handler.close()


def test_live_interrupted_endpoint_shape(client):
    body = client.get("/api/live/interrupted").json()
    assert isinstance(body["sessions"], list)
