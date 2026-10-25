"""Instrument master — the mapping from a symbol you can say to a key the API accepts.

Upstox publishes the full contract list as gzipped JSON, refreshed daily and
served without authentication. Contracts change every day (options are born and
expire constantly), so this is synced, not hardcoded.

`instrument_key` is the identifier every other Upstox endpoint wants. Everything
here exists to get you from "BANKNIFTY 52000 CE this week" to "NSE_FO|43219".
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Literal

import httpx

from backend.core.config import IST, settings

log = logging.getLogger(__name__)

BASE = "https://assets.upstox.com/market-quote/instruments/exchange"

Exchange = Literal["NSE", "BSE", "MCX", "NCDEX", "complete"]

# Index instrument keys are stable and worth naming, since they are the
# underlyings everything else hangs off and the raw keys are unreadable.
INDICES = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANKEX": "BSE_INDEX|BANKEX",
}


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_key: str
    trading_symbol: str
    name: str
    exchange: str
    segment: str
    instrument_type: str  # EQ | FUT | CE | PE | INDEX
    lot_size: int
    tick_size: float
    expiry: date | None
    strike_price: float | None
    underlying_key: str | None
    underlying_symbol: str | None

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("CE", "PE")

    @property
    def is_derivative(self) -> bool:
        return self.instrument_type in ("CE", "PE", "FUT")


def _epoch_ms_to_date(value: object) -> date | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=IST).date()
    except (TypeError, ValueError, OSError):
        return None


def _parse(rec: dict) -> Instrument | None:
    key = rec.get("instrument_key")
    if not key:
        return None
    try:
        return Instrument(
            instrument_key=key,
            trading_symbol=rec.get("trading_symbol") or rec.get("name") or "",
            name=rec.get("name") or "",
            exchange=rec.get("exchange") or "",
            segment=rec.get("segment") or "",
            instrument_type=rec.get("instrument_type") or "",
            lot_size=int(rec.get("lot_size") or 1),
            tick_size=float(rec.get("tick_size") or 0.05),
            expiry=_epoch_ms_to_date(rec.get("expiry")),
            strike_price=(
                float(rec["strike_price"]) if rec.get("strike_price") not in (None, "") else None
            ),
            underlying_key=rec.get("underlying_key"),
            underlying_symbol=rec.get("underlying_symbol"),
        )
    except (TypeError, ValueError) as exc:
        log.debug("Skipping malformed instrument %s: %s", key, exc)
        return None


def download(exchange: Exchange = "complete") -> list[Instrument]:
    """Fetch and parse the contract list. No auth required."""
    url = f"{BASE}/{exchange}.json.gz"
    log.info("Downloading instrument master: %s", url)
    resp = httpx.get(url, timeout=120.0, follow_redirects=True)
    resp.raise_for_status()

    raw = json.loads(gzip.decompress(resp.content))
    out = [inst for rec in raw if (inst := _parse(rec)) is not None]
    log.info("Parsed %d instruments from %s (%d raw records)", len(out), exchange, len(raw))
    return out


def sync(exchange: Exchange = "complete") -> int:
    """Download and persist into DuckDB. Returns the row count written.

    Replaces the table wholesale rather than merging: the published file is the
    source of truth, and a stale contract lingering in our table would be a
    silent source of orders against instruments that no longer trade.
    """
    instruments = download(exchange)
    if not instruments:
        raise RuntimeError(f"Instrument master for {exchange} came back empty")
    return replace_all(instruments)


def replace_all(instruments: list[Instrument]) -> int:
    """Persist a full contract list, replacing whatever was stored."""
    from backend.data.store import get_db

    rows = [
        (
            i.instrument_key, i.trading_symbol, i.name, i.exchange, i.segment,
            i.instrument_type, i.lot_size, i.tick_size, i.expiry,
            i.strike_price, i.underlying_key, i.underlying_symbol,
        )
        for i in instruments
    ]

    db = get_db()
    db.execute("BEGIN TRANSACTION")
    try:
        db.execute("DELETE FROM instruments")
        db.executemany(
            """INSERT INTO instruments
               (instrument_key, trading_symbol, name, exchange, segment,
                instrument_type, lot_size, tick_size, expiry,
                strike_price, underlying_key, underlying_symbol)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        db.execute(
            "INSERT OR REPLACE INTO meta VALUES ('instruments_synced_at', ?)",
            [datetime.now(IST).isoformat()],
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    log.info("Synced %d instruments", len(rows))
    return len(rows)


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------

def resolve(symbol: str) -> str:
    """Turn a friendly name into an instrument_key.

    Accepts an alias ("NIFTY"), a raw key ("NSE_INDEX|Nifty 50"), or an exact
    trading symbol ("RELIANCE").
    """
    if "|" in symbol:
        return symbol
    upper = symbol.upper().replace(" ", "")
    if upper in INDICES:
        return INDICES[upper]

    from backend.data.store import get_db

    row = get_db().execute(
        """SELECT instrument_key FROM instruments
           WHERE upper(trading_symbol) = ? AND instrument_type = 'EQ'
           ORDER BY exchange = 'NSE' DESC LIMIT 1""",
        [upper],
    ).fetchone()
    if row:
        return row[0]
    raise KeyError(f"Cannot resolve symbol {symbol!r}. Has the instrument master been synced?")


def option_chain(underlying: str, expiry: date) -> list[Instrument]:
    """All CE/PE contracts for an underlying and expiry, ordered by strike."""
    from backend.data.store import get_db

    rows = get_db().execute(
        """SELECT instrument_key, trading_symbol, name, exchange, segment,
                  instrument_type, lot_size, tick_size, expiry,
                  strike_price, underlying_key, underlying_symbol
           FROM instruments
           WHERE underlying_symbol = ? AND expiry = ? AND instrument_type IN ('CE','PE')
           ORDER BY strike_price, instrument_type""",
        [underlying.upper(), expiry],
    ).fetchall()
    return [Instrument(*r) for r in rows]


def expiries(underlying: str, on_or_after: date | None = None) -> list[date]:
    """Available option expiries for an underlying, soonest first."""
    from backend.data.store import get_db

    cutoff = on_or_after or datetime.now(IST).date()
    rows = get_db().execute(
        """SELECT DISTINCT expiry FROM instruments
           WHERE underlying_symbol = ? AND instrument_type IN ('CE','PE')
             AND expiry >= ?
           ORDER BY expiry""",
        [underlying.upper(), cutoff],
    ).fetchall()
    return [r[0] for r in rows]


def lot_size(instrument_key: str) -> int:
    from backend.data.store import get_db

    row = get_db().execute(
        "SELECT lot_size FROM instruments WHERE instrument_key = ?", [instrument_key]
    ).fetchone()
    return int(row[0]) if row else 1


def search(text: str, limit: int = 20, tradeable_only: bool = True) -> list[Instrument]:
    """Search for the UI's instrument picker.

    Ranked by match quality, not by symbol length. Ordering by length alone puts
    "RIIL" and "RCOM" above "RELIANCE" when you type RELIANCE, which makes the
    picker feel broken even though the match is technically valid.

    Cash equity and indices rank above derivatives: typing "RELIANCE" almost
    always means the stock, not one of its 400 option contracts.
    """
    from backend.data.store import get_db

    needle = text.upper().strip()
    type_filter = (
        "AND instrument_type IN ('EQ','INDEX','FUT')" if tradeable_only else ""
    )

    rows = get_db().execute(
        f"""SELECT instrument_key, trading_symbol, name, exchange, segment,
                   instrument_type, lot_size, tick_size, expiry,
                   strike_price, underlying_key, underlying_symbol
            FROM instruments
            WHERE (upper(trading_symbol) LIKE ? OR upper(name) LIKE ?)
              {type_filter}
            ORDER BY
              CASE
                WHEN upper(trading_symbol) = ?        THEN 0
                WHEN upper(name) = ?                  THEN 1
                WHEN upper(trading_symbol) LIKE ?     THEN 2
                WHEN upper(name) LIKE ?               THEN 3
                ELSE 4
              END,
              CASE instrument_type
                WHEN 'INDEX' THEN 0 WHEN 'EQ' THEN 1 ELSE 2
              END,
              exchange = 'NSE' DESC,
              length(trading_symbol)
            LIMIT ?""",
        [
            f"%{needle}%", f"%{needle}%",
            needle, needle,
            f"{needle}%", f"{needle}%",
            limit,
        ],
    ).fetchall()
    return [Instrument(*r) for r in rows]


def count() -> int:
    from backend.data.store import get_db

    return get_db().execute("SELECT count(*) FROM instruments").fetchone()[0]


def iter_underlyings(segment: str) -> Iterable[str]:
    from backend.data.store import get_db

    for (sym,) in get_db().execute(
        "SELECT DISTINCT underlying_symbol FROM instruments WHERE segment = ? "
        "AND underlying_symbol IS NOT NULL ORDER BY 1",
        [segment],
    ).fetchall():
        yield sym


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    n = sync()
    print(f"\n{n:,} instruments synced")
    print(f"NIFTY  -> {resolve('NIFTY')}")
    print(f"Next BANKNIFTY expiries: {expiries('BANKNIFTY')[:5]}")
