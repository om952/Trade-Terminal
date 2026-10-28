"""Fingerprints of the bars a run used, so a result can be checked against its data.

A run stores which instrument, timeframe and range it read, how many bars, and
a hash of their values. Re-running after the data was discarded and downloaded
again can then confirm it got the same bars, or show that it didn't (for
example because the exchange revised a candle).
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd

FINGERPRINT_VERSION = 1
_VALUE_COLUMNS = ("open", "high", "low", "close", "volume")


def fingerprint(df: pd.DataFrame, instrument_key: str, timeframe: str) -> dict:
    """Describe `df` (canonical candle columns) in a way that survives a re-download."""
    out: dict = {
        "version": FINGERPRINT_VERSION,
        "instrument_key": instrument_key,
        "timeframe": timeframe,
        "rows": int(len(df)),
        "first_bar": None,
        "last_bar": None,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    if df.empty:
        return out

    timestamps = pd.to_datetime(df["timestamp"], utc=True)
    digest = hashlib.sha256()
    # Nanoseconds since the epoch in UTC, then each value column as float64:
    # independent of the time zone the frame happens to be in, and of int/float
    # dtype differences between a fresh download and a stored file.
    digest.update(np.ascontiguousarray(timestamps.astype("int64").to_numpy(), dtype="<i8").tobytes())
    for column in _VALUE_COLUMNS:
        values = pd.to_numeric(df[column], errors="coerce").astype("float64").to_numpy()
        digest.update(np.ascontiguousarray(values, dtype="<f8").tobytes())

    out.update(
        first_bar=timestamps.iloc[0].isoformat(),
        last_bar=timestamps.iloc[-1].isoformat(),
        sha256=digest.hexdigest(),
    )
    return out


def same_data(a: dict | None, b: dict | None) -> bool:
    """Whether two fingerprints describe the same bars."""
    if not a or not b:
        return False
    keys = ("version", "instrument_key", "timeframe", "rows", "first_bar", "last_bar", "sha256")
    return all(a.get(k) == b.get(k) for k in keys)
