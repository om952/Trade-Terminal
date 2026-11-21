"""NSE's daily list of securities in the F&O ban period (P5.2).

When a stock's open interest across exchanges passes 95% of its market-wide
position limit, it enters the "ban period": no new F&O positions in it may be
opened until it falls back under 80%, and positions may only be reduced. NSE's
risk checks for algorithmic orders include this (the MWPL check, NSE/INVG/69255
para 11.1 item 10; docs/REGULATORY_CHECK.md section 6), so the pre-trade guard
refuses new F&O positions in a banned underlying.

The ban applies to derivatives only. Cash-market (NSE_EQ) trades in the same
stock are unaffected, and indices are never in the ban list.

The source (checked 21 November 2026):

* ``https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_DDMMYYYY.csv``
  is the list for that trade date. A date with no list (a weekend or holiday, or
  a date not yet published) returns HTTP 404 with an HTML page.
* ``https://nsearchives.nseindia.com/content/fo/fo_secban.csv`` is the latest
  list, which after the close already names the *next* trade date. It is used
  only when its header names the date asked for.

The format is plain text. The first line names the trade date, and each banned
security follows as ``serial,SYMBOL``::

    Securities in Ban For Trade Date 15-JAN-2025:
    1,AARTIIND
    2,ANGELONE

A day with no banned securities is a single line ending in ``NIL``::

    Securities in Ban For Trade Date 03-FEB-2025: NIL

A list is used only when its header names the trade date asked for. A list
that is cached, parsed and verified is never fetched again. A fetch that fails,
or a file that does not parse, gives ``available=False`` with the reason, and
:func:`is_banned` then answers None. Callers fail closed on None: an unknown
ban status is not a clean bill of health.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from backend.core.config import IST, settings

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_{day:%d%m%Y}.csv"
LATEST_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"
TIMEOUT_S = 15.0

# Index underlyings are never in the ban list (NSE and BSE index derivatives).
INDEX_UNDERLYINGS = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50",
})

_HEADER = re.compile(r"^Securities in Ban For Trade Date (\d{2}-[A-Za-z]{3}-\d{4}):\s*(NIL)?\s*$")
_ROW = re.compile(r"^\s*\d+\s*,\s*([A-Za-z0-9&\-_.]+)\s*$")


@dataclass(frozen=True)
class BanList:
    as_of: date | None
    symbols: frozenset[str]
    available: bool
    source: str
    reason: str = ""


class _Unparseable(ValueError):
    pass


def _parse(text: str) -> tuple[date, frozenset[str]]:
    """The trade date and banned symbols in one of NSE's files."""
    lines = [line.strip() for line in text.replace("\r", "").split("\n") if line.strip()]
    if not lines:
        raise _Unparseable("the file is empty")
    header = _HEADER.match(lines[0])
    if header is None:
        raise _Unparseable(f"unexpected first line {lines[0][:80]!r}")
    day = datetime.strptime(header.group(1).upper(), "%d-%b-%Y").date()
    if header.group(2):
        if len(lines) > 1:
            raise _Unparseable("a NIL list has more lines after it")
        return day, frozenset()
    symbols = []
    for line in lines[1:]:
        row = _ROW.match(line)
        if row is None:
            raise _Unparseable(f"unexpected line {line[:80]!r}")
        symbols.append(row.group(1).upper())
    return day, frozenset(symbols)


def _default_fetcher(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed https URL
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return response.read()


def _cache_path(day: date) -> Path:
    return settings.data_dir / "fo_ban" / f"{day.isoformat()}.csv"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _today() -> date:
    return datetime.now(IST).date()


def ban_list(day: date | None = None, *, fetch: bool = True,
             fetcher: Callable[[str], bytes] | None = None) -> BanList:
    """The F&O ban list for trade date `day` (default: today in IST). Never raises for network errors."""
    day = day or _today()
    cached = _cache_path(day)
    if cached.exists():
        try:
            as_of, symbols = _parse(cached.read_text(encoding="utf-8"))
            if as_of == day:
                return BanList(as_of, symbols, True, f"cache:{cached.name}")
            log.warning("Cached F&O ban list %s names %s; fetching again", cached.name, as_of)
        except (OSError, _Unparseable, ValueError) as exc:
            log.warning("Cached F&O ban list %s is unreadable (%s); fetching again", cached.name, exc)
    if not fetch:
        return BanList(None, frozenset(), False, "cache", f"no cached F&O ban list for {day}")

    get = fetcher or _default_fetcher
    problems = []
    for url in (ARCHIVE_URL.format(day=day), LATEST_URL):
        try:
            text = get(url).decode("utf-8", errors="replace")
            as_of, symbols = _parse(text)
        except _Unparseable as exc:
            problems.append(f"{url}: not a ban list ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - any network failure means "unknown", never a crash
            problems.append(f"{url}: {exc}")
            continue
        if as_of != day:
            problems.append(f"{url}: the list is for {as_of}, not {day}")
            continue
        try:
            _write_atomic(cached, text)
        except OSError as exc:
            log.warning("Could not cache the F&O ban list for %s: %s", day, exc)
        return BanList(as_of, symbols, True, url)
    reason = f"no F&O ban list for {day} (a non-trading day, or not published yet): " + "; ".join(problems)
    log.warning("%s", reason)
    return BanList(None, frozenset(), False, "nse", reason)


def _underlying(symbol_or_key: str) -> tuple[str | None, bool]:
    """(underlying symbol, is an index) for an F&O key or a plain symbol. (None, False) if unknown."""
    if "|" not in symbol_or_key:
        name = symbol_or_key.strip().upper()
        return name, name in INDEX_UNDERLYINGS
    from backend.data.store import read

    row = read("SELECT underlying_symbol, underlying_key FROM instruments WHERE instrument_key = ?",
               [symbol_or_key]).fetchone()
    if row is None or not row[0]:
        return None, False
    name, key = str(row[0]).upper(), str(row[1] or "")
    return name, name in INDEX_UNDERLYINGS or "_INDEX|" in key


def is_banned(symbol_or_key: str, day: date | None = None, *, lists: BanList | None = None) -> bool | None:
    """Whether new F&O positions in this instrument's underlying are barred on `day`.

    Accepts a trading symbol (``RELIANCE``) or an instrument key. Index
    underlyings, cash-market keys (NSE_EQ, BSE_EQ: the ban applies only to
    F&O positions), index keys and synthetic (SIM|) keys are never banned. An
    F&O key is resolved to its underlying through the instrument master.
    None means unknown: the list is unavailable, or the F&O instrument is not
    in the instrument master. Callers fail closed on None.
    """
    if symbol_or_key.startswith("SIM|") or "_INDEX|" in symbol_or_key or "_EQ|" in symbol_or_key:
        return False
    underlying, index = _underlying(symbol_or_key)
    if index:
        return False
    if underlying is None:
        return None
    current = lists if lists is not None else ban_list(day)
    if not current.available:
        return None
    return underlying in current.symbols
