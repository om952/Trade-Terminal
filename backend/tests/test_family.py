"""P2.8: trial counting per strategy family."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date

import pytest

from backend.appstate import db, runs
from backend.core.config import settings
from backend.strategy import templates
from backend.strategy.family import describe, family_key
from backend.strategy.schema import Strategy


def _doc(key="ema_crossover", **over):
    doc = templates.load(key, symbol="SIM|FAM").model_dump(mode="json")
    doc.update(over)
    return doc


def _key(doc) -> str:
    return family_key(Strategy.model_validate(doc))


BASE = _doc()


def _variant(change) -> dict:
    doc = copy.deepcopy(BASE)
    change(doc)
    return doc


# ---------------------------------------------------------------------------
# What stays in a family
# ---------------------------------------------------------------------------

SAME = {
    "indicator periods": lambda d: d["indicators"][0]["params"].update(period=13),
    "name, id and description": lambda d: d.update(name="other", id="stg_x", description="y"),
    "instrument": lambda d: d["instrument"].update(symbol="NSE_INDEX|Nifty Bank"),
    "timeframe": lambda d: d.update(timeframe="15m"),
    "session times": lambda d: d["session"].update(start="09:45", end="14:30", square_off="15:00"),
    "stop size": lambda d: d["legs"][0].update(stop_loss={"type": "atr_multiple", "value": 3.5, "ref": "atr"}),
    "lots": lambda d: d["legs"][0].update(lots=5),
    "risk values": lambda d: d["risk"].update(max_trades_per_day=9),
    "re-entry values": lambda d: d["risk"]["re_entry"].update(max=7, cooldown_bars=1),
    "slippage": lambda d: d["costs"].update(slippage_bps=20),
    "indicator ids": lambda d: _rename_indicator(d, d["indicators"][0]["id"], "fast_line"),
    "condition order": lambda d: d["entry"]["conditions"].reverse(),
}


def _rename_indicator(doc, old, new):
    text = json.dumps(doc).replace(f'"{old}"', f'"{new}"')
    doc.clear()
    doc.update(json.loads(text))


@pytest.mark.parametrize("what", SAME)
def test_tuning_stays_in_the_family(what):
    assert _key(_variant(SAME[what])) == _key(BASE), what


def test_flipped_and_nested_conditions_stay_in_the_family():
    a = _doc("macd_crossover")
    b = copy.deepcopy(a)
    first, second = b["entry"]["conditions"]
    b["entry"]["conditions"] = [
        {"op": "AND", "conditions": [{"left": second["right"], "cmp": "<", "right": second["left"]}]},
        first,
    ]
    assert _key(a) == _key(b)


def test_numeric_thresholds_are_parameters():
    a = _doc("rsi_mean_reversion")
    b = copy.deepcopy(a)
    b["entry"]["conditions"][0]["right"] = 25
    b["exit"]["conditions"][0]["right"] = 70
    assert _key(a) == _key(b)


def test_strike_offsets_and_expiry_are_parameters():
    a = _doc("bull_call_spread")
    b = copy.deepcopy(a)
    b["legs"][1]["option"].update(strike_rule="OTM+4", expiry="next_weekly")
    assert _key(a) == _key(b)


# ---------------------------------------------------------------------------
# What makes a new family
# ---------------------------------------------------------------------------

DIFFERENT = {
    "another indicator type": lambda d: d["indicators"][0].update(type="SMA"),
    "an extra filter": lambda d: d["entry"]["conditions"].append({"left": "close", "cmp": ">", "right": 0}),
    "AND becomes OR": lambda d: d["entry"].update(op="OR") if len(d["entry"]["conditions"]) > 1
    else d["entry"]["conditions"].append({"left": "close", "cmp": ">", "right": 0}) or d["entry"].update(op="OR"),
    "a different comparator": lambda d: d["entry"]["conditions"][0].update(cmp=">"),
    "the other side": lambda d: d["legs"][0].update(action="SELL"),
    "no stop loss": lambda d: d["legs"][0].update(stop_loss=None),
    "a target added": lambda d: d["legs"][0].update(target={"type": "percent", "value": 1.0}),
    "positional": lambda d: d["session"].update(square_off=None),
    "a new risk rule": lambda d: d["risk"].update(max_loss_per_day=5000),
    "no exit signal": lambda d: d.update(exit=None),
    "an indicator on volume": lambda d: d["indicators"][0]["params"].update(source="volume"),
}


@pytest.mark.parametrize("what", DIFFERENT)
def test_changing_the_idea_makes_a_new_family(what):
    assert _key(_variant(DIFFERENT[what])) != _key(BASE), what


def test_option_structure_matters():
    call_spread, put_spread = _doc("bull_call_spread"), _doc("bear_put_spread")
    assert _key(call_spread) != _key(put_spread)
    flipped = copy.deepcopy(call_spread)
    flipped["legs"][1]["option"]["strike_rule"] = "STRIKE:25000"
    assert _key(flipped) != _key(call_spread)


def test_shipped_templates_are_all_distinct_families():
    keys = {t["key"]: _key(_doc(t["key"])) for t in templates.list_templates()}
    assert len(set(keys.values())) == len(keys), keys


def test_describe_is_plain_json_and_key_is_stable():
    shape = describe(Strategy.model_validate(BASE))
    json.dumps(shape)
    assert _key(BASE) == _key(copy.deepcopy(BASE)) and _key(BASE).startswith("fam_")


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------

@pytest.fixture
def appdb(tmp_path, monkeypatch):
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield tmp_path / "app.sqlite"
    db.close()
    store.close_db()


def _record(doc):
    strategy = Strategy.model_validate(doc)
    return runs.record(kind="backtest", document=doc, params={}, payload={
        "strategy_hash": strategy.content_hash(), "metrics": {"sharpe": 1.0}, "data": {}})


def test_runs_are_counted_per_family(appdb):
    other = _variant(DIFFERENT["another indicator type"])
    tuned = _variant(SAME["indicator periods"])
    for doc in (BASE, BASE, tuned, other):
        _record(doc)
    family = _key(BASE)
    assert runs.family_trials(family) == {"runs": 3, "variants": 2}
    assert runs.family_trials(_key(other)) == {"runs": 1, "variants": 1}
    assert {r["family"] for r in runs.list_runs(family=family)} == {family}
    listed = {f["family"]: f for f in runs.families()}
    assert listed[family]["runs"] == 3 and listed[family]["variants"] == 2


def test_backtests_report_both_counts(appdb):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.data.simulator import generate_ohlcv
    from backend.data.store import write_candles

    write_candles("SIM|FAM", "15m", generate_ohlcv("NIFTY", date(2025, 1, 1), date(2025, 6, 30),
                                                   "15m", seed=2), source="simulated")
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    doc = dict(BASE, timeframe="15m")
    body = {"strategy": doc, "include_chart": False}
    first = client.post("/api/backtest", json=body).json()
    retuned = copy.deepcopy(doc)
    retuned["indicators"][0]["params"]["period"] = 11
    second = client.post("/api/backtest", json={**body, "strategy": retuned}).json()

    assert first["family"] == second["family"] == _key(doc)
    assert first["trials"] == 1 and second["trials"] == 1
    assert second["family_trials"] == {"runs": 2, "variants": 2}
    listed = client.get("/api/families").json()["families"]
    assert listed[0]["family"] == first["family"] and listed[0]["runs"] == 2
    assert len(client.get(f"/api/runs?family={first['family']}").json()["runs"]) == 2


def test_migration_backfills_runs_recorded_before_families(appdb):
    """A database at schema 2 (no family column) upgrades with every run assigned."""
    conn = sqlite3.connect(appdb)
    for number, step in enumerate(db.MIGRATIONS[:2], 1):
        conn.executescript(step)
        conn.execute(f"PRAGMA user_version = {number}")
    for i, doc in enumerate((BASE, _variant(SAME["lots"]), {"name": "unreadable"})):
        conn.execute(
            """INSERT INTO runs (id, kind, strategy_hash, strategy_name, document, params, data,
                                 metrics, result, created_at)
               VALUES (?, 'backtest', 'h', 'n', ?, '{}', '{}', '{}', x'00', ?)""",
            [f"run_{i}", json.dumps(doc), f"2025-01-0{i + 1}"],
        )
    conn.commit()
    conn.close()

    db._migrated.discard(appdb)
    assert db.connection().execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    families = dict(db.connection().execute("SELECT id, family FROM runs").fetchall())
    assert families["run_0"] == families["run_1"] == _key(BASE)
    assert families["run_2"] is None, "an unreadable document must not block the upgrade"


def test_a_failing_migration_leaves_the_database_untouched(appdb, monkeypatch):
    db.connection()
    db.close()
    db._migrated.discard(appdb)

    def broken(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise RuntimeError("migration bug")

    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS, broken])
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(db.MIGRATIONS))
    with pytest.raises(RuntimeError, match="migration bug"):
        db.connection()
    db.close()
    raw = sqlite3.connect(appdb)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
    assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name = 'half_done'").fetchone()[0] == 0
    raw.close()
