"""The final holdout: data each strategy family may test on exactly once (P4.2).

Every backtest, sweep and validation run is a look at the data, and a family
that is tuned against the same history long enough will fit its noise. The
deflated Sharpe ratio corrects for how many looks there were; the holdout is
the stronger guarantee: a slice of history no ordinary backtest is allowed to
see, so the one test on it is a genuine out-of-sample test.

Rules, enforced here and in `backtest.run`, which every way of running a
backtest goes through (the UI, the chat builder, sweeps, MCP):

* The holdout is every session from `start()` on. The date is fixed the first
  time it is needed (today minus HOLDOUT_MONTHS) and stored, so it never moves
  and the data behind it stays unseen.
* An ordinary backtest of real market data ends the day before the holdout,
  and says so. Synthetic (SIM|) data is exempt: it says nothing about markets.
* `final_test` runs a strategy on the holdout once per family. The claim is
  taken before the run, so two at once cannot both get in; a run that fails
  to produce a result (no data, an error) releases it.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Any

from backend.appstate.db import connection, transaction
from backend.core.config import IST, settings

_final: ContextVar[bool] = ContextVar("holdout_final_test", default=False)


class HoldoutUsed(Exception):
    """This family has had its look at the holdout."""

    def __init__(self, family: str, run_id: str | None, when: str) -> None:
        super().__init__(f"This idea has already had its final test ({when[:10]}"
                         f"{', run ' + run_id if run_id else ', still running'}).")
        self.family, self.run_id, self.when = family, run_id, when


def _months_before(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month + 1, 1)


def start(today: date | None = None) -> date | None:
    """The first held-out session, fixed on first use. None when the holdout is off."""
    if settings.holdout_months <= 0:
        return None
    row = connection().execute("SELECT value FROM app_meta WHERE key = 'holdout_start'").fetchone()
    if row is not None:
        return date.fromisoformat(row["value"])
    chosen = _months_before(today or datetime.now(IST).date(), settings.holdout_months)
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO app_meta (key, value) VALUES ('holdout_start', ?)",
                     [chosen.isoformat()])
        stored = conn.execute("SELECT value FROM app_meta WHERE key = 'holdout_start'").fetchone()
    return date.fromisoformat(stored["value"])


def applies(instrument_key: str) -> bool:
    return not instrument_key.startswith("SIM|")


def bounds(instrument_key: str, first: date | None, last: date | None) -> tuple[date | None, date | None, dict | None]:
    """The (start, end) a backtest may use, and what the holdout did to them."""
    held = start() if applies(instrument_key) else None
    if held is None:
        return first, last, None
    if _final.get():
        return max(first or held, held), last, {"start": held.isoformat(), "final": True,
                                                "note": f"Final test on the held-out data from {held}."}
    if first is not None and first >= held:
        raise ValueError(
            f"Data from {held} is held out for one final test per idea; this range starts inside it. "
            "End the backtest before that date, or run the final test."
        )
    end = held - timedelta(days=1)
    if last is not None and last < held:
        return first, last, {"start": held.isoformat(), "final": False, "note": None}
    return first, end, {"start": held.isoformat(), "final": False,
                        "note": f"Data from {held} is held out for one final test per idea, so this backtest ends on {end}."}


def used(family: str) -> dict[str, Any] | None:
    row = connection().execute("SELECT * FROM holdout_tests WHERE family = ?", [family]).fetchone()
    return None if row is None else dict(row)


def _claim(family: str, held: date) -> None:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO holdout_tests (family, holdout_start, created_at) VALUES (?,?,?)",
            [family, held.isoformat(), datetime.now(IST).isoformat(timespec="seconds")],
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT * FROM holdout_tests WHERE family = ?", [family]).fetchone()
            raise HoldoutUsed(family, row["run_id"], row["created_at"])


def final_test(params, *, strategy_id: str | None = None, on_progress=None) -> dict[str, Any]:
    """Run a strategy once on the held-out data. Raises HoldoutUsed on a second try."""
    from backend.services import backtest
    from backend.services.jobs import backtest_and_record
    from backend.strategy.family import family_key

    held = start()
    if held is None:
        raise ValueError("The holdout is off (HOLDOUT_MONTHS=0); there is nothing held out to test on.")
    strategy = backtest.validate(params.strategy)
    key = backtest.resolve(params.symbol or strategy.instrument.symbol)
    if not applies(key):
        raise ValueError("Synthetic data has no holdout; the final test is for real market data.")
    family = family_key(strategy.model_dump(mode="json"))
    _claim(family, held)
    token = _final.set(True)
    try:
        payload = backtest_and_record(params, strategy_id=strategy_id, on_progress=on_progress)
    except BaseException:
        with transaction() as conn:          # no result: the look was not taken
            conn.execute("DELETE FROM holdout_tests WHERE family = ? AND run_id IS NULL", [family])
        raise
    finally:
        _final.reset(token)
    with transaction() as conn:
        conn.execute("UPDATE holdout_tests SET run_id = ?, strategy_hash = ? WHERE family = ?",
                     [payload["run_id"], payload["strategy_hash"], family])
    return payload


def status() -> dict[str, Any]:
    held = start()
    tested = connection().execute("SELECT count(*) FROM holdout_tests WHERE run_id IS NOT NULL").fetchone()[0]
    return {"enabled": held is not None, "start": held.isoformat() if held else None,
            "months": settings.holdout_months, "families_tested": tested}
