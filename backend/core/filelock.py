"""A lock that also holds across processes, for read-merge-write on shared files.

The API and the data CLIs can run at the same time and write the same candle
series. Atomic replace (see backend/data/candles.py) keeps a file from ever
being half-written; this lock keeps two merges from racing, where the second
would silently drop the first one's bars.

POSIX uses flock, Windows uses msvcrt; both are released by the OS if the
process dies, so a crash can't leave a stale lock behind.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


_thread_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` (created if needed) for the block.

    flock is per open file, so threads of one process would not exclude each
    other through it alone; a per-path thread lock covers that case.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _guard:
        thread_lock = _thread_locks.setdefault(key, threading.Lock())
    with thread_lock:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
