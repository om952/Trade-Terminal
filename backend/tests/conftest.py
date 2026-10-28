"""Test isolation: every run gets its own data directory and no developer secrets.

This module is imported by pytest before any test module, which is what makes it
work: `backend.core.config` builds `settings` at import time, so the environment
must be set before anything imports it. Nothing in the suite may touch the
repo's real `data/` folder or read the developer's `.env` (live tokens, Telegram
credentials, account risk limits).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="trade-terminal-tests-"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["TRADE_TERMINAL_ENV_FILE"] = ""

import pytest  # noqa: E402

from backend.core.config import ROOT, settings  # noqa: E402

if settings.data_dir.resolve() == (ROOT / "data").resolve():
    raise RuntimeError(
        "Test run is pointed at the repo's real data/ folder. "
        "backend.core.config was imported before backend/tests/conftest.py set DATA_DIR."
    )


def pytest_sessionfinish(session, exitstatus) -> None:
    from backend.data import store

    store.close_db()
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def instrument_master() -> None:
    """A small contract list, so instrument lookups work without a broker sync."""
    from datetime import date

    from backend.data.instruments import Instrument, replace_all

    def inst(key, symbol, name, itype, exchange="NSE", segment="NSE_EQ", lot=1, **kw):
        return Instrument(
            instrument_key=key, trading_symbol=symbol, name=name, exchange=exchange,
            segment=segment, instrument_type=itype, lot_size=lot, tick_size=0.05,
            expiry=kw.get("expiry"), strike_price=kw.get("strike"),
            underlying_key=kw.get("underlying_key"), underlying_symbol=kw.get("underlying"),
        )

    replace_all([
        inst("NSE_EQ|INE002A01018", "RELIANCE", "RELIANCE INDUSTRIES LTD", "EQ"),
        inst("BSE_EQ|INE002A01018", "RELIANCE", "RELIANCE INDUSTRIES LTD", "EQ",
             exchange="BSE", segment="BSE_EQ"),
        inst("NSE_EQ|INE013A01015", "RELINFRA", "RELIANCE INFRASTRUCTURE LTD", "EQ"),
        inst("NSE_EQ|INE036A01016", "RIIL", "RELIANCE INDUSTRIAL INFRA", "EQ"),
        inst("NSE_FO|50001", "RELIANCE FUT 30 DEC 26", "RELIANCE", "FUT",
             segment="NSE_FO", lot=500, expiry=date(2026, 12, 30),
             underlying_key="NSE_EQ|INE002A01018", underlying="RELIANCE"),
        inst("NSE_INDEX|Nifty 50", "NIFTY", "Nifty 50", "INDEX", segment="NSE_INDEX"),
    ])
