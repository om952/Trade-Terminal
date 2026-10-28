"""Strategy library and run history: versions by content hash, templates, trials."""

from __future__ import annotations

import pytest

from backend.app.context import AppContext
from backend.app.library import NotFound, Provenance, ReadOnly, StrategyLibrary
from backend.app.runs import RunStore
from backend.strategy import templates


@pytest.fixture
def ctx(tmp_path) -> AppContext:
    return AppContext(user_id="local", data_dir=tmp_path)


@pytest.fixture
def lib(ctx) -> StrategyLibrary:
    return StrategyLibrary(ctx)


@pytest.fixture
def runs(ctx) -> RunStore:
    return RunStore(ctx)


def _ema(**changes):
    strategy = templates.load("ema_crossover")
    return strategy.model_copy(update=changes) if changes else strategy


def _with_period(strategy, period: int):
    doc = strategy.model_dump(mode="json")
    doc["indicators"][0]["params"]["period"] = period
    return type(strategy).model_validate(doc)


def test_create_and_read_back(lib):
    record = lib.create(_ema(), "builder", name="My EMA")
    assert record.name == "My EMA"
    assert record.version_count == 1
    head = lib.version(record.id)
    assert head.hash == _ema().content_hash()
    assert head.strategy().name == "My EMA"  # library and document names agree
    assert head.parent_hash is None


def test_behaviour_change_adds_a_linked_version(lib):
    record = lib.create(_ema(), "builder")
    edited = _with_period(_ema(), 12)
    version = lib.save_version(record.id, edited, "builder", note="faster EMA")

    assert version.hash == edited.content_hash() != record.head_hash
    assert version.parent_hash == record.head_hash
    assert lib.get(record.id).head_hash == edited.content_hash()
    assert [v.hash for v in lib.versions(record.id)] == [edited.content_hash(), record.head_hash]


def test_rename_only_does_not_add_a_version(lib):
    record = lib.create(_ema(), "builder")
    lib.save_version(record.id, _ema(name="Renamed", description="new words"), "builder")
    assert lib.get(record.id).version_count == 1
    assert lib.get(record.id).name == "Renamed"
    assert lib.version(record.id).document["description"] == "new words"


def test_saving_an_earlier_version_moves_head_back_without_duplicating(lib):
    record = lib.create(_ema(), "builder")
    lib.save_version(record.id, _with_period(_ema(), 12), "builder")
    lib.save_version(record.id, _ema(), "builder")  # undo

    assert lib.get(record.id).head_hash == _ema().content_hash()
    assert lib.get(record.id).version_count == 2


def test_llm_provenance_is_recorded(lib):
    record = lib.create(_ema(), "llm", provenance=Provenance(
        model="claude-opus-5-5", prompt_version="compile-v1", conversation_id="conv_1"))
    head = lib.version(record.id)
    assert (head.source, head.model, head.prompt_version, head.conversation_id) == (
        "llm", "claude-opus-5-5", "compile-v1", "conv_1")


def test_templates_import_once_and_are_read_only(lib):
    added = lib.import_templates()
    assert added == len(templates.TEMPLATES)
    assert lib.import_templates() == 0  # idempotent

    template = lib.get("tpl_ema_crossover")
    assert template.read_only and template.source == "template"
    with pytest.raises(ReadOnly):
        lib.save_version(template.id, _with_period(_ema(), 12), "builder")
    with pytest.raises(ReadOnly):
        lib.rename(template.id, "mine now")
    with pytest.raises(ReadOnly):
        lib.delete(template.id)


def test_fork_of_template_starts_new_family_and_is_editable(lib):
    lib.import_templates()
    fork = lib.fork("tpl_ema_crossover", name="My take")
    assert not fork.read_only and fork.source == "builder"
    assert fork.family_id == fork.id != lib.get("tpl_ema_crossover").family_id
    lib.save_version(fork.id, _with_period(_ema(), 12), "builder")


def test_fork_of_own_strategy_stays_in_family(lib):
    original = lib.create(_ema(), "builder")
    fork = lib.fork(original.id)
    assert fork.family_id == original.family_id
    assert fork.name.endswith("(copy)")


def test_archive_and_delete(lib, runs):
    record = lib.create(_ema(), "builder")
    run_id = runs.start("backtest", record.head_hash, strategy_id=record.id)
    lib.set_archived(record.id, True)
    assert record.id not in [s.id for s in lib.list_strategies()]
    assert record.id in [s.id for s in lib.list_strategies(include_archived=True)]

    lib.delete(record.id)
    with pytest.raises(NotFound):
        lib.get(record.id)
    # The run survives, still traceable by content hash.
    kept = runs.get(run_id)
    assert kept.strategy_id is None and kept.strategy_hash == record.head_hash


def test_run_lifecycle(runs):
    rid = runs.start("backtest", "abc", params={"symbol": "SIM|X", "lot_size": 50})
    assert runs.get(rid).status == "running"
    runs.finish(rid, metrics={"net_pnl": 1234.5}, data_fingerprint={"rows": 10, "sha256": "f00"})
    run = runs.get(rid)
    assert run.status == "done" and run.metrics == {"net_pnl": 1234.5}
    assert run.data_fingerprint == {"rows": 10, "sha256": "f00"}
    assert run.params == {"lot_size": 50, "symbol": "SIM|X"}

    failed = runs.start("backtest", "abc")
    runs.fail(failed, "no data")
    assert runs.get(failed).status == "failed" and runs.get(failed).error == "no data"


def test_trial_counter_counts_completed_backtests_per_family(lib, runs):
    record = lib.create(_ema(), "builder")
    v2 = lib.save_version(record.id, _with_period(_ema(), 12), "builder")
    for content_hash in (record.head_hash, record.head_hash, v2.hash):
        runs.finish(runs.start("backtest", content_hash, strategy_id=record.id))
    runs.fail(runs.start("backtest", v2.hash, strategy_id=record.id), "x")  # not a trial
    runs.finish(runs.start("paper", v2.hash, strategy_id=record.id))       # not a backtest

    assert runs.trials(record.family_id) == {"backtests": 3, "distinct_versions": 2}


def test_backtests_before_saving_join_the_family_when_saved(lib, runs):
    strategy = _ema()
    runs.finish(runs.start("backtest", strategy.content_hash()))  # unsaved
    record = lib.create(strategy, "builder")
    assert runs.trials(record.family_id)["backtests"] == 1
    assert runs.list_runs(strategy_id=record.id)[0].family_id == record.family_id


def test_users_do_not_see_each_others_strategies(tmp_path):
    alice = StrategyLibrary(AppContext(user_id="alice", data_dir=tmp_path))
    bob = StrategyLibrary(AppContext(user_id="bob", data_dir=tmp_path))
    record = alice.create(_ema(), "builder")
    assert bob.list_strategies() == []
    with pytest.raises(NotFound):
        bob.get(record.id)
