"""Many backtests at once, in worker processes (P4.4).

The engine is pure Python, so backtests on threads take turns on one core. A
sweep, a robustness check and a research run each need tens of backtests, so
they go through `backtest_many`, which spreads the engine work over a process
pool. The division of labour keeps the storage rules intact:

* The app's process loads every request's bars (`backtest.prepare`). DuckDB
  allows one writer process, and the holdout date lives in the app state, so
  workers never open either: they get the bars and the document, run
  `backtest.compute`, and send back the report. A worker that tries to open a
  store fails loudly (`_forbid_stores`).
* Results come back to the app's process, which records them in SQLite in
  request order, so trial counts and deflated Sharpe ratios come out exactly
  as they would one at a time.
* Each task runs under a fixed seed derived from its strategy and data
  (`seed_for`), set before the engine starts. The engine has no randomness
  today; the seed makes any that is added reproducible no matter which worker
  runs the task or in what order.

The same request gives the same report whether it ran in the app's process
or in a worker, with one worker or many (backend/tests/test_pool.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing
import os
import random
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from backend.core.config import settings
from backend.services import backtest

log = logging.getLogger(__name__)

Progress = Callable[[float, str], None]

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_pool_size = 0


def workers() -> int:
    """How many worker processes batches use. 1 runs everything in the app's process."""
    if settings.backtest_workers > 0:
        return settings.backtest_workers
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def seed_for(prepared: backtest.Prepared) -> int:
    """A seed fixed by what is being tested: the document, the settings and the data."""
    params = prepared.params.model_dump(mode="json")
    text = json.dumps({"params": params, "data": prepared.data.get("fingerprint")}, sort_keys=True, default=str)
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)


def _seeded_compute(prepared: backtest.Prepared, seed: int) -> dict[str, Any]:
    import numpy as np

    random.seed(seed)
    np.random.seed(seed % 2**32)
    payload = backtest.compute(prepared)
    payload["seed"] = seed
    return payload


def _forbid_stores() -> None:
    """Worker initializer: the stores belong to the app's process."""
    from backend.appstate import db
    from backend.data import store

    def refuse(*_: Any, **__: Any) -> Any:
        raise RuntimeError("A backtest worker process must not open the data or app store; "
                           "the app's process loads bars and records results.")

    store.get_db = refuse
    db.connection = refuse
    os.environ["TRADE_TERMINAL_WORKER"] = "1"


def _executor(size: int) -> ProcessPoolExecutor:
    global _pool, _pool_size
    with _lock:
        if _pool is None or _pool_size != size:
            if _pool is not None:
                _pool.shutdown(wait=False, cancel_futures=True)
            # spawn, not fork: the app's process has threads and open DuckDB
            # and SQLite handles, none of which survive a fork safely.
            _pool = ProcessPoolExecutor(max_workers=size, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_forbid_stores)
            _pool_size = size
        return _pool


def _discard() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None


def shutdown() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=True)
            _pool = None


def backtest_many(
    requests: Sequence[backtest.BacktestParams],
    *,
    on_progress: Progress | None = None,
    labels: Sequence[str] | None = None,
    size: int | None = None,
    noun: str = "Backtest",
) -> list[dict[str, Any] | Exception]:
    """Run every request; return each report, or the exception it raised, in request order.

    One failing request does not stop the rest. `on_progress(fraction,
    message)` may raise to cancel: requests not yet started are dropped and
    the exception propagates with `.partial`, the results so far in request
    order (None where unfinished), so the caller can keep what finished. A
    request already running in a worker finishes there and is discarded.
    """
    n = len(requests)
    size = size or workers()
    names = list(labels) if labels is not None else [str(i + 1) for i in range(n)]
    results: list[dict[str, Any] | Exception | None] = [None] * n
    try:
        _run(requests, results, names, size, noun, on_progress)
    except BaseException as exc:
        exc.partial = list(results)        # type: ignore[attr-defined]
        raise
    return [r if r is not None else RuntimeError("not run") for r in results]


def _run(requests: Sequence[backtest.BacktestParams], results: list, names: list[str], size: int, noun: str,
         on_progress: Progress | None) -> None:
    n = len(requests)

    def report(fraction: float, message: str) -> None:
        if on_progress is not None:
            on_progress(fraction, message)

    # Load in the app's process, once per instrument, timeframe and range.
    frames: dict[tuple, Any] = {}
    prepared: dict[int, backtest.Prepared] = {}
    for i, req in enumerate(requests):
        report(0.0, f"Loading data: {noun.lower()} {i + 1} of {n}")
        try:
            prepared[i] = backtest.prepare(req, frames=frames)
        except Exception as exc:  # noqa: BLE001 - reported per request
            results[i] = exc
    done = n - len(prepared)

    def inline(items: list[tuple[int, backtest.Prepared]]) -> None:
        nonlocal done
        for i, p in items:
            report(done / max(1, n), f"{noun} {i + 1} of {n}: {names[i]}")
            try:
                results[i] = _seeded_compute(p, seed_for(p))
            except Exception as exc:  # noqa: BLE001
                results[i] = exc
            done += 1

    if size <= 1:
        inline(list(prepared.items()))
        return

    queue = list(prepared.items())
    running: dict[Future, int] = {}
    try:
        pool = _executor(size)
        while queue or running:
            # At most two tasks per worker in flight: bounded memory, quick cancel.
            while queue and len(running) < 2 * size:
                i, p = queue.pop(0)
                running[pool.submit(_seeded_compute, p, seed_for(p))] = i
            ready, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in sorted(ready, key=running.__getitem__):
                i = running.pop(future)
                error = future.exception()
                if isinstance(error, BrokenProcessPool):
                    raise error
                results[i] = error if isinstance(error, Exception) else future.result()
                done += 1
                report(done / max(1, n), f"{done} of {n} {noun.lower()}s done (latest: {names[i]})")
    except BrokenProcessPool:
        # A worker died (killed, out of memory). The pool is unusable; the next
        # batch builds a new one, and this one finishes in the app's process.
        log.warning("A backtest worker process died; finishing %d backtest(s) in the app's process",
                    sum(1 for i in prepared if results[i] is None))
        _discard()
        inline([(i, p) for i, p in prepared.items() if results[i] is None])
    finally:
        for future in running:
            future.cancel()
