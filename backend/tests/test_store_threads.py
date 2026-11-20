"""The shared DuckDB connection is not safe across threads; every read takes its own cursor.

`get_db().execute(q).df()` runs q on the shared connection and then fetches
"the last result" of that connection. When another thread queries in between,
the fetch comes back empty (None) or holds the other query's rows. The UI
fires /api/datasets twice at once on every load, and jobs read the instrument
master from worker threads, so this race happened in practice.
"""

from __future__ import annotations

import threading
from datetime import date

import pytest

from backend.core.config import settings


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    from backend.data import store
    from backend.data.simulator import generate_ohlcv

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    for n in range(6):
        frame = generate_ohlcv("RELIANCE", date(2025, 1, 1), date(2025, 3, 1), "1d", seed=n)
        store.write_candles(f"SIM|T{n}", "1d", frame, source="simulated")
    db = store.get_db()
    for n in range(50):
        db.execute(
            "INSERT INTO instruments VALUES (?, ?, ?, 'NSE', 'NSE_FO', 'FUT', 1, 0.05, NULL, NULL, ?, ?)",
            [f"NSE_FO|{n}", f"S{n} FUT", f"S{n}", f"NSE_EQ|E{n}", f"S{n}"],
        )
    yield
    store.close_db()


def test_concurrent_reads_never_see_another_threads_result(seeded):
    from backend.data import instruments as inst
    from backend.data import store
    from backend.scan import service as scan
    from backend.scan.spec import Universe

    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def hammer(kind: int) -> None:
        start.wait()
        try:
            for _ in range(150):
                if kind % 4 == 0:
                    coverage = store.all_coverage()
                    assert len(coverage) == 6 and "instrument_key" in coverage.columns
                elif kind % 4 == 1:
                    assert inst.count() == 50
                elif kind % 4 == 2:
                    assert len(store.stored_instruments("1d")) == 6
                else:
                    assert len(scan.resolve_universe(Universe(preset="fno_stocks"))) == 50
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, f"{len(errors)} thread(s) failed; first: {errors[0]!r}"
