"""Test isolation: the suite must never touch the real data directory.

`backend.core.config` builds `settings` (and creates its directories) at import
time, so the redirect has to happen here, at conftest import, before any test
module pulls the backend in. Environment variables outrank `.env` in
pydantic-settings, which is what makes this override stick.

The same block blanks the outward-facing credentials. A developer's `.env` may
hold a working Telegram bot token and an Upstox access token; a test run must
not be able to send a real alert or call the broker with them.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

assert "backend.core.config" not in sys.modules, (
    "backend.core.config was imported before conftest could redirect DATA_DIR; "
    "the suite would write into the real data folder."
)

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="trade-terminal-tests-"))

os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["UPSTOX_ACCESS_TOKEN"] = ""

from backend.core.config import ROOT, settings  # noqa: E402

_REAL_DATA_DIR = (ROOT / "data").resolve()


def _inside_real_data_dir(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == _REAL_DATA_DIR or _REAL_DATA_DIR in resolved.parents


def pytest_sessionstart(session: pytest.Session) -> None:
    if _inside_real_data_dir(settings.data_dir):
        pytest.exit(
            f"Refusing to run: tests would write into the real data directory "
            f"({settings.data_dir}). conftest.py must redirect DATA_DIR first.",
            returncode=3,
        )


@pytest.fixture(autouse=True)
def _data_dir_stays_isolated():
    """Catch a test that repoints the data dir at the repo's own folder."""
    yield
    assert not _inside_real_data_dir(settings.data_dir), (
        f"A test left settings.data_dir pointing at the real data folder: {settings.data_dir}"
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    from backend.data import store

    store.close_db()
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
