"""App sessions and the session data cache: keep, discard, ask, idle, crash, promote."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from backend.app import preferences
from backend.app.app_session import AppSessions, scratch_store
from backend.app.context import AppContext
from backend.core.config import IST
from backend.data.candles import library_store
from backend.data.service import DataService
from backend.tests.test_data_service import KEY, FakeBroker

START = date(2026, 2, 2)
END = date(2026, 2, 13)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.combine(date(2026, 3, 13), time(18, 0), tzinfo=IST)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> AppContext:
    from backend.core.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return AppContext(user_id="local", data_dir=tmp_path)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sessions(ctx, clock) -> AppSessions:
    return AppSessions(ctx, now=clock)


def _download(sessions: AppSessions, ctx, broker=None, start=START, end=END):
    broker = broker or FakeBroker()
    read, write = sessions.stores()
    service = DataService(ctx, read_stores=read, write_store=write, fetcher=broker,
                          now=lambda: datetime.combine(date(2026, 3, 13), time(18, 0), tzinfo=IST))
    df, _, report = service.get_bars(KEY, "5m", start, end)
    return df, report, broker


def _files(root) -> list:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_downloads_land_in_the_session_cache_not_the_library(sessions, ctx):
    session_id = sessions.begin()
    df, report, _ = _download(sessions, ctx)
    assert report.fetched_bars == len(df) > 0
    assert library_store(ctx).all_series() == []
    assert scratch_store(session_id, ctx).info(KEY, "5m").rows == len(df)
    info = sessions.info()
    assert info["scratch_bytes"] > 0 and info["series"][0]["instrument_key"] == KEY


def test_a_second_strategy_in_the_same_session_downloads_nothing(sessions, ctx):
    sessions.begin()
    _, _, broker = _download(sessions, ctx)
    _, report, broker2 = _download(sessions, ctx)
    assert report.fetched_bars == 0 and broker2.calls == []


def test_quit_with_keep_moves_data_into_the_library(sessions, ctx):
    session_id = sessions.begin()
    df, _, _ = _download(sessions, ctx)
    assert sessions.end("quit", "keep")["outcome"] == "kept"
    assert library_store(ctx).info(KEY, "5m").rows == len(df)
    assert not (ctx.data_dir / "scratch" / session_id).exists()

    # The next session reuses it with no download at all.
    sessions.begin()
    _, report, broker = _download(sessions, ctx)
    assert report.fetched_bars == 0 and broker.calls == []


def test_quit_with_discard_leaves_no_file_behind(sessions, ctx):
    session_id = sessions.begin()
    _download(sessions, ctx)
    assert sessions.end("quit", "discard")["outcome"] == "discarded"
    assert _files(ctx.data_dir / "scratch" / session_id) == []
    assert _files(ctx.data_dir / "store") == []
    assert library_store(ctx).all_series() == []


def test_remembered_choice_is_saved_and_applied_next_time(sessions, ctx):
    sessions.begin()
    _download(sessions, ctx)
    sessions.end("quit", "discard", remember=True)
    assert preferences.get("data_retention", ctx) == "discard"

    session_id = sessions.begin()
    _download(sessions, ctx)
    assert sessions.end("shutdown")["outcome"] == "discarded"  # nobody asked
    assert _files(ctx.data_dir / "scratch" / session_id) == []


def test_always_keep_writes_straight_to_the_library(sessions, ctx):
    preferences.set("data_retention", "keep", ctx)
    session_id = sessions.begin()
    df, _, _ = _download(sessions, ctx)
    assert library_store(ctx).info(KEY, "5m").rows == len(df)
    assert scratch_store(session_id, ctx).all_series() == []
    assert sessions.end("quit")["outcome"] == "discarded"  # nothing was left in scratch


def test_ask_with_nobody_to_ask_leaves_the_data_pending(sessions, ctx):
    session_id = sessions.begin()
    df, _, _ = _download(sessions, ctx)
    assert sessions.end("shutdown")["outcome"] == "pending"
    pending = sessions.pending()
    assert [p["id"] for p in pending] == [session_id]
    assert pending[0]["series"][0]["bars"] == len(df) and pending[0]["end_cause"] == "shutdown"

    assert sessions.resolve(session_id, "keep")["outcome"] == "kept"
    assert sessions.pending() == []
    assert library_store(ctx).info(KEY, "5m").rows == len(df)


def test_empty_session_needs_no_decision(sessions, ctx):
    sessions.begin()
    assert sessions.end("shutdown")["outcome"] == "discarded"
    assert sessions.pending() == []


def test_a_crashed_session_is_offered_at_the_next_start(ctx, clock):
    crashed = AppSessions(ctx, now=clock)
    crashed_id = crashed.begin()
    _download(crashed, ctx)
    # The process dies: end() never runs. A new process starts.
    fresh = AppSessions(ctx, now=clock)
    fresh.begin()
    pending = fresh.pending()
    assert [p["id"] for p in pending] == [crashed_id]
    assert pending[0]["end_cause"] == "crash"


def test_idle_session_ends_after_the_timeout_unless_paper_is_running(sessions, ctx, clock):
    first = sessions.begin()
    clock.now += timedelta(minutes=29)
    assert not sessions.check_idle(paper_running=False)
    clock.now += timedelta(minutes=2)
    assert not sessions.check_idle(paper_running=True)  # a paper session keeps it alive
    assert sessions.check_idle(paper_running=False)
    assert sessions.current_id is None

    # The UI coming back starts a fresh session.
    second = sessions.heartbeat()
    assert second != first


def test_heartbeat_keeps_the_session_alive(sessions, clock):
    session_id = sessions.begin()
    for _ in range(4):
        clock.now += timedelta(minutes=20)
        sessions.heartbeat()
        assert not sessions.check_idle(paper_running=False)
    assert sessions.current_id == session_id


def test_interrupted_promotion_resumes_without_losing_data(sessions, ctx, monkeypatch):
    session_id = sessions.begin()
    _download(sessions, ctx)
    other = "NSE_EQ|SECOND"
    read, write = sessions.stores()
    DataService(ctx, read_stores=read, write_store=write, fetcher=FakeBroker(),
                now=lambda: datetime.combine(date(2026, 3, 13), time(18, 0), tzinfo=IST)
                ).get_bars(other, "5m", START, END)
    sessions.end("shutdown")  # ask -> pending
    assert len(scratch_store(session_id, ctx).all_series()) == 2

    from backend.data import candles

    real_write = candles.CandleStore.write
    calls = {"n": 0}

    def flaky(self, key, tf, df, source):
        if self.name == "library":
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
        return real_write(self, key, tf, df, source)

    monkeypatch.setattr(candles.CandleStore, "write", flaky)
    with pytest.raises(OSError):
        sessions.resolve(session_id, "keep")
    monkeypatch.setattr(candles.CandleStore, "write", real_write)

    # One series made it, the other is still safe in scratch, still pending.
    assert len(library_store(ctx).all_series()) == 1
    assert len(scratch_store(session_id, ctx).all_series()) == 1
    assert [p["id"] for p in sessions.pending()] == [session_id]

    assert sessions.resolve(session_id, "keep")["outcome"] == "kept"
    assert {s.instrument_key for s in library_store(ctx).all_series()} == {KEY, other}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_app_session_endpoints(ctx):
    from fastapi.testclient import TestClient

    from backend.api.main import app
    from backend.app.app_session import get_app_sessions

    client = TestClient(app, base_url="http://localhost")
    beat = client.post("/api/app/heartbeat").json()["session"]
    assert beat["session_id"] and beat["data_retention"] == "ask" and beat["idle_minutes"] == 30
    assert client.get("/api/app/session").json()["session"]["session_id"] == beat["session_id"]

    result = client.post("/api/app/quit", json={"decision": "discard", "remember": False,
                                                "exit": False}).json()
    assert result["outcome"] == "discarded" and result["exiting"] is False
    assert get_app_sessions(ctx).current_id is None
    assert client.get("/api/app/pending").json() == {"pending": []}
    assert client.post("/api/app/pending/as_nope", json={"decision": "keep"}).status_code == 404
