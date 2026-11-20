"""Research memory: which ideas were tried on which instrument and regime, and how they did.

Every candidate a research run evaluates is recorded with its outcome and the
reason. The next plan on the same instrument is shown the failures, and the
screen drops a proposal whose family already failed there in the same regime,
so the search does not keep paying for the same dead end.

The regime is a coarse description of the data the candidate was tested on:
its trend over the period and its volatility. It is deliberately crude; it is
there so "failed in a falling, volatile market" is not read as "failed".
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import pandas as pd

from backend.appstate.db import connection, transaction
from backend.core.config import IST

TREND_BAND = 0.05        # annualized return within +-5%: flat
HIGH_VOL = 0.20          # annualized volatility above 20%: high


def regime(frame: pd.DataFrame) -> str:
    """'up, low volatility' and the like, from a bar frame's daily closes."""
    if frame is None or frame.empty or len(frame) < 2:
        return "unknown"
    ts = pd.to_datetime(frame["timestamp"])
    closes = frame.assign(_day=ts.dt.date).groupby("_day")["close"].last()
    if len(closes) < 2 or closes.iloc[0] <= 0:
        return "unknown"
    years = max((closes.index[-1] - closes.index[0]).days / 365.25, 1 / 365.25)
    growth = (closes.iloc[-1] / closes.iloc[0]) ** (1 / years) - 1
    vol = float(closes.pct_change().dropna().std() * math.sqrt(252)) if len(closes) > 2 else 0.0
    trend = "up" if growth > TREND_BAND else "down" if growth < -TREND_BAND else "flat"
    return f"{trend}, {'high' if vol > HIGH_VOL else 'low'} volatility"


def record(session_id: str, base_family: str, instrument: str, rows: list[dict[str, Any]]) -> None:
    now = datetime.now(IST).isoformat(timespec="seconds")
    with transaction() as conn:
        conn.executemany(
            """INSERT INTO research_memory (session_id, base_family, family, instrument, regime, hypothesis,
                                            outcome, reason, sharpe, dsr, run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(session_id, base_family, r["family"], instrument, r["regime"], r["hypothesis"], r["outcome"],
              r["reason"], r.get("sharpe"), r.get("dsr"), r.get("run_id"), now) for r in rows],
        )


def failures(instrument: str, limit: int = 30) -> list[dict[str, Any]]:
    """Recent failed candidates on an instrument, newest first."""
    rows = connection().execute(
        """SELECT family, regime, hypothesis, reason, sharpe, dsr, created_at FROM research_memory
           WHERE instrument = ? AND outcome = 'failed' ORDER BY id DESC LIMIT ?""",
        [instrument, max(1, min(limit, 200))],
    ).fetchall()
    return [dict(r) for r in rows]


def failed_before(family: str, instrument: str, regime_tag: str) -> dict[str, Any] | None:
    row = connection().execute(
        """SELECT hypothesis, reason, created_at FROM research_memory
           WHERE family = ? AND instrument = ? AND regime = ? AND outcome = 'failed'
           ORDER BY id DESC LIMIT 1""",
        [family, instrument, regime_tag],
    ).fetchone()
    return None if row is None else dict(row)


def prior_sharpes(base_family: str, instrument: str, exclude_session: str) -> list[float | None]:
    """Sharpe of every candidate earlier research on this base and instrument evaluated.

    Those were tries too: a search run in two sessions is still one search.
    """
    rows = connection().execute(
        """SELECT sharpe FROM research_memory
           WHERE base_family = ? AND instrument = ? AND session_id != ?""",
        [base_family, instrument, exclude_session],
    ).fetchall()
    return [r["sharpe"] for r in rows]
