"""P2.1: the eval set and harness check themselves before they judge a model."""

from __future__ import annotations

import copy
import json

import pytest

from backend.evals import runner
from backend.evals.canonical import canonical, strike
from backend.evals.score import Outcome, gold_strategy, score, scored_fields
from backend.llm.base import LLMError
from backend.llm.fake import FakeLLM
from backend.strategy.schema import Strategy

CASES = runner.load_cases()
BY_ID = {c["id"]: c for c in CASES}


@pytest.fixture(autouse=True, scope="module")
def empty_instrument_master(tmp_path_factory):
    """Symbol resolution depends on the instrument master, so give these tests
    their own empty one rather than whatever earlier tests left in the shared
    store. (A real eval run uses the synced master; gold and answer resolve
    through it identically.)"""
    from backend.core.config import settings
    from backend.data import store

    original = settings.data_dir
    store.close_db()
    settings.data_dir = tmp_path_factory.mktemp("evals")
    yield
    store.close_db()
    settings.data_dir = original


def _strategy(**over) -> Strategy:
    doc = {
        "name": "t", "instrument": {"symbol": "NIFTY", "segment": "index"}, "timeframe": "5m",
        "indicators": [{"id": "ema9", "type": "EMA", "params": {"period": 9}},
                       {"id": "ema21", "type": "EMA", "params": {"period": 21}}],
        "entry": {"op": "AND", "conditions": [{"left": "ema9", "cmp": "crosses_above", "right": "ema21"},
                                               {"left": "close", "cmp": ">", "right": "ema21"}]},
        "legs": [{"id": "leg1", "action": "BUY", "kind": "equity", "lots": 1}],
    }
    doc.update(over)
    return Strategy.model_validate(doc)


def _oracle_answer(case) -> dict:
    """What a perfect model would return for this case."""
    status = case["expect"]["status"]
    base = {"questions": [], "assumptions": [], "strategy_json": "", "unsupported_reason": ""}
    if status == "ready":
        doc = dict(case["expect"]["document"])
        doc.setdefault("name", case["id"])
        return {**base, "status": "ready", "strategy_json": json.dumps(doc)}
    if status == "needs_clarification":
        return {**base, "status": status,
                "questions": [{"question": "Which?", "why": "open", "options": ["a", "b"]}]}
    return {**base, "status": "unsupported", "unsupported_reason": case["expect"]["note"]}


# ---------------------------------------------------------------------------
# The set itself
# ---------------------------------------------------------------------------

def test_the_set_is_well_formed():
    assert 50 <= len(CASES) <= 100
    assert len(BY_ID) == len(CASES), "duplicate case ids"
    holdout = sum(c["split"] == "holdout" for c in CASES) / len(CASES)
    assert 0.25 <= holdout <= 0.35, f"holdout share {holdout:.0%}"
    statuses = {c["expect"]["status"] for c in CASES}
    assert statuses == {"ready", "needs_clarification", "unsupported"}
    assert any("hinglish" in c["tags"] for c in CASES) and any("options" in c["tags"] for c in CASES)
    for split in ("dev", "holdout"):
        in_split = [c for c in CASES if c["split"] == split]
        assert {c["expect"]["status"] for c in in_split} == statuses, f"{split} lacks a status"


@pytest.mark.parametrize("case", [c for c in CASES if c["expect"]["status"] == "ready"],
                         ids=lambda c: c["id"])
def test_every_gold_document_is_valid_and_fully_scored(case):
    strategy = gold_strategy(case)
    canonical(strategy)
    assert {"entry", "legs", "instrument"} <= set(scored_fields(case))


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_a_perfect_answer_scores_perfectly(case):
    """Gold through the real graph: cases, canonical form, scorer and runner agree."""
    outcome, answers = runner.run_case(case, FakeLLM([_oracle_answer(case)]))
    result = score(case, outcome)
    assert result.exact, result.mismatches or (result.status, outcome.error)
    assert outcome.llm_calls == 1 and outcome.repairs == 0 and len(answers) == 1


# ---------------------------------------------------------------------------
# Canonical form: same meaning, same form
# ---------------------------------------------------------------------------

def test_spelling_differences_do_not_matter():
    base = canonical(_strategy())
    renamed = _strategy(
        indicators=[{"id": "slow", "type": "EMA", "params": {"period": 21, "source": "close"}},
                    {"id": "fast", "type": "EMA", "params": {"period": 9}}],
        entry={"op": "AND", "conditions": [
            {"left": "slow", "cmp": "<", "right": "close"},                     # flipped and reordered
            {"op": "AND", "conditions": [{"left": "slow", "cmp": "crosses_below", "right": "fast"}]},
        ]},
    )
    assert canonical(renamed)["entry"] == base["entry"]

    inline = _strategy(
        indicators=[{"id": "ema9", "type": "EMA", "params": {"period": 9}}],
        entry={"op": "AND", "conditions": [
            {"left": "ema9", "cmp": "crosses_above", "right": {"type": "EMA", "of": "close", "period": 21}},
            {"left": "close", "cmp": ">", "right": {"type": "EMA", "period": 21}},
        ]},
    )
    assert canonical(inline)["entry"] == base["entry"]
    assert canonical(_strategy(timeframe="60m"))["timeframe"] == canonical(_strategy(timeframe="1h"))["timeframe"]
    assert canonical(_strategy(instrument={"symbol": "NSE_INDEX|Nifty 50", "segment": "index"}))[
        "instrument"] == base["instrument"]


def test_meaning_differences_do_matter():
    base = canonical(_strategy())
    looser = _strategy(entry={"op": "AND", "conditions": [
        {"left": "ema9", "cmp": "crosses_above", "right": "ema21"},
        {"left": "close", "cmp": ">=", "right": "ema21"}]})
    assert canonical(looser)["entry"] != base["entry"]
    either = _strategy(entry={"op": "OR", "conditions": [
        {"left": "ema9", "cmp": "crosses_above", "right": "ema21"},
        {"left": "close", "cmp": ">", "right": "ema21"}]})
    assert canonical(either)["entry"] != base["entry"]
    other_period = _strategy(indicators=[{"id": "ema9", "type": "EMA", "params": {"period": 10}},
                                          {"id": "ema21", "type": "EMA", "params": {"period": 21}}])
    assert canonical(other_period)["entry"] != base["entry"]
    assert canonical(_strategy(legs=[{"id": "leg1", "action": "BUY", "kind": "equity", "lots": 2}]))[
        "legs"] != base["legs"]


def test_strike_rules_that_resolve_alike_compare_alike():
    assert strike("OTM-2") == strike("otm+2") == "OTM+2"
    assert strike("ATM") == strike("ATM+0") and strike("OTM") == strike("OTM+1")
    assert strike("ATM+2") != strike("ATM-2") != strike("OTM+2")
    assert strike("DELTA:0.2") == strike("DELTA:0.20") and strike("STRIKE:25000") == strike("STRIKE:25000.0")


def test_the_always_true_entry_is_recognized():
    timed = _strategy(entry={"op": "AND", "conditions": [{"left": "close", "cmp": ">", "right": 0}]})
    also = _strategy(entry={"op": "OR", "conditions": [{"left": 0, "cmp": "<", "right": "close"}]})
    assert canonical(timed)["entry"] == canonical(also)["entry"] == ("always",)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _gold_doc(case_id):
    doc = copy.deepcopy(BY_ID[case_id]["expect"]["document"])
    doc.setdefault("name", case_id)
    return doc


def test_a_wrong_field_is_named_and_the_rest_still_score():
    case = BY_ID["rsi-oversold-bounce"]
    doc = _gold_doc(case["id"])
    doc["legs"][0]["stop_loss"]["value"] = 1.0
    result = score(case, Outcome(status="ready", document=doc))
    assert result.valid and not result.exact
    assert result.fields == {"instrument": True, "timeframe": True, "entry": True, "exit": True, "legs": False}
    assert "legs" in result.mismatches


def test_only_what_the_prompt_states_is_scored():
    case = BY_ID["cpr-breakout"]                       # states max 2 trades a day, nothing else
    doc = _gold_doc(case["id"])
    doc["risk"] = {"max_trades_per_day": 2, "max_loss_per_day": 9999, "max_concurrent_positions": 3}
    doc["session"] = {"start": "10:00", "end": "14:00", "square_off": "14:30"}
    assert score(case, Outcome(status="ready", document=doc)).exact
    doc["risk"]["max_trades_per_day"] = 3
    assert score(case, Outcome(status="ready", document=doc)).fields["risk"] is False

    timed = BY_ID["short-straddle-930"]                 # timeframe is not stated
    doc = _gold_doc(timed["id"])
    doc["timeframe"] = "15m"
    doc["session"]["end"] = "15:00"                     # an end time is not stated either
    assert score(timed, Outcome(status="ready", document=doc)).exact
    doc["session"]["square_off"] = "15:20"
    assert score(timed, Outcome(status="ready", document=doc)).fields["session"] is False


def test_asking_when_it_should_compile_is_a_miss_and_vice_versa():
    ready_case, ask_case = BY_ID["ema-cross-nifty-5m"], BY_ID["clarify-no-instrument"]
    over_asked = score(ready_case, Outcome(status="needs_clarification"))
    assert not over_asked.status_ok and not over_asked.exact and not over_asked.valid
    guessed = score(ask_case, Outcome(status="ready", document=_gold_doc("ema-cross-nifty-5m")))
    assert not guessed.exact
    assert score(ask_case, Outcome(status="needs_clarification")).exact
    assert score(BY_ID["unsupported-news"], Outcome(status="unsupported")).exact


def test_an_invalid_document_scores_as_invalid():
    result = score(BY_ID["ema-cross-nifty-5m"], Outcome(status="ready", document={"name": "x"}))
    assert not result.valid and not result.exact and "document" in result.mismatches


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def test_repairs_and_failures_are_counted():
    case = BY_ID["ema-cross-nifty-5m"]
    bad = _oracle_answer(case)
    broken = json.loads(bad["strategy_json"])
    broken["legs"] = []
    bad = {**bad, "strategy_json": json.dumps(broken)}

    outcome, _ = runner.run_case(case, FakeLLM([bad, _oracle_answer(case)]))
    assert outcome.status == "ready" and outcome.repairs == 1 and outcome.llm_calls == 2

    failed, _ = runner.run_case(case, FakeLLM([LLMError("boom")]))
    assert failed.status == "failed" and "boom" in failed.error
    assert not score(case, failed).exact


def test_summary_and_report():
    cases = [BY_ID["ema-cross-nifty-5m"], BY_ID["rsi-oversold-bounce"], BY_ID["clarify-no-instrument"]]
    wrong = _oracle_answer(cases[1])
    doc = json.loads(wrong["strategy_json"])
    doc["exit"]["conditions"][0]["right"] = 70
    answers = {cases[0]["id"]: [_oracle_answer(cases[0])],
               cases[1]["id"]: [{**wrong, "strategy_json": json.dumps(doc)}],
               cases[2]["id"]: [_oracle_answer(cases[2])]}
    report = runner.run(cases, lambda c: FakeLLM(list(answers[c["id"]])), "claude-opus-5-5")
    s = report["summary"]
    assert s["cases"] == 3 and s["exact_pct"] == 66.7 and s["status_pct"] == 100.0
    assert s["valid_pct"] == 100.0 and s["field_pct"]["exit"] == 50.0 and s["field_pct"]["entry"] == 100.0
    # FakeLLM reports 1000 in / 500 out per call: 3 calls at $4 / $20 per MTok.
    assert s["cost_usd_total"] == pytest.approx(3 * (1000 * 4 + 500 * 20) / 1e6)
    text = runner.markdown(report, "dev", "test")
    assert "rsi-oversold-bounce" in text and "| exit | 50.0% |" in text


def test_record_then_replay_gives_the_same_scores(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(runner, "REPORTS_DIR", tmp_path / "reports")
    cases = runner.load_cases(split="holdout", limit=4)
    live = runner.run(cases, lambda c: FakeLLM([_oracle_answer(c)]), "claude-opus-5-5")
    recording = tmp_path / "rec.json"
    recording.write_text(json.dumps({"model": "claude-opus-5-5", "answers": live["recordings"]}))

    assert runner.main(["--split", "holdout", "--limit", "4", "--replay", str(recording)]) == 0
    assert "Exact 100.0%" in capsys.readouterr().out
    assert list((tmp_path / "reports").glob("*.md"))


def test_a_live_run_needs_explicit_consent(capsys):
    assert runner.main(["--split", "dev", "--limit", "2"]) == 2
    assert "--confirm-spend" in capsys.readouterr().out
