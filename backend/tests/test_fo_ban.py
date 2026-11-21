"""P5.2: NSE's daily F&O ban list (backend/data/fo_ban.py). No network: every fetch is injected."""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.config import settings
from backend.data import fo_ban

DAY = date(2025, 1, 15)
# Byte for byte as NSE serves archives/fo/sec_ban/fo_secban_15012025.csv (checked 21 Nov 2026).
SAMPLE = (b"Securities in Ban For Trade Date 15-JAN-2025:\n1,AARTIIND\n2,ANGELONE\n3,BANDHANBNK\n"
          b"4,HINDCOPPER\n5,LTF\n6,RBLBANK\n")
NIL = b"Securities in Ban For Trade Date 03-FEB-2025: NIL\n"
NOT_FOUND = b"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<title>404</title>"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend.appstate import db
    from backend.data import store

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store.close_db()
    yield tmp_path
    db.close()
    store.close_db()


class Fetcher:
    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self.responses, self.calls = responses, []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        found = self.responses.get(url, OSError("HTTP Error 404: Not Found"))
        if isinstance(found, Exception):
            raise found
        return found


def _archive(day: date) -> str:
    return fo_ban.ARCHIVE_URL.format(day=day)


def test_the_real_format_parses(env):
    fetch = Fetcher({_archive(DAY): SAMPLE})
    got = fo_ban.ban_list(DAY, fetcher=fetch)
    assert got.available and got.as_of == DAY and got.source == _archive(DAY)
    assert got.symbols == frozenset({"AARTIIND", "ANGELONE", "BANDHANBNK", "HINDCOPPER", "LTF", "RBLBANK"})


def test_a_day_with_no_banned_securities_is_available_and_empty(env):
    day = date(2025, 2, 3)
    got = fo_ban.ban_list(day, fetcher=Fetcher({_archive(day): NIL}))
    assert got.available and got.as_of == day and got.symbols == frozenset()


def test_the_cache_is_reused_without_a_second_fetch(env):
    fetch = Fetcher({_archive(DAY): SAMPLE})
    first = fo_ban.ban_list(DAY, fetcher=fetch)
    again = fo_ban.ban_list(DAY, fetcher=fetch)
    assert fetch.calls == [_archive(DAY)]
    assert again.symbols == first.symbols and again.source.startswith("cache:")
    assert (env / "fo_ban" / "2025-01-15.csv").read_bytes() == SAMPLE
    assert fo_ban.ban_list(DAY, fetch=False).available, "the cache answers without the network"


def test_the_latest_file_is_used_only_when_it_names_the_day(env):
    day = date(2026, 11, 23)
    latest = b"Securities in Ban For Trade Date 23-NOV-2026:\n1,MANAPPURAM\n"
    got = fo_ban.ban_list(day, fetcher=Fetcher({fo_ban.LATEST_URL: latest}))
    assert got.available and got.symbols == {"MANAPPURAM"} and got.source == fo_ban.LATEST_URL
    other = fo_ban.ban_list(date(2026, 11, 24), fetcher=Fetcher({fo_ban.LATEST_URL: latest}))
    assert not other.available and "not 2026-11-24" in other.reason


def test_a_failed_fetch_is_unavailable_never_raised_and_not_cached(env):
    fetch = Fetcher({_archive(DAY): NOT_FOUND, fo_ban.LATEST_URL: TimeoutError("timed out")})
    got = fo_ban.ban_list(DAY, fetcher=fetch)
    assert not got.available and got.symbols == frozenset() and "timed out" in got.reason
    assert not (env / "fo_ban" / "2025-01-15.csv").exists()
    assert fo_ban.is_banned("RELIANCE", DAY, lists=got) is None, "unknown, so callers fail closed"
    assert not fo_ban.ban_list(DAY, fetch=False).available


def test_an_unparseable_cache_is_fetched_again(env):
    path = env / "fo_ban" / "2025-01-15.csv"
    path.parent.mkdir(parents=True)
    path.write_text("garbage")
    got = fo_ban.ban_list(DAY, fetcher=Fetcher({_archive(DAY): SAMPLE}))
    assert got.available and "RBLBANK" in got.symbols


def test_symbols_indices_and_cash(env):
    lists = fo_ban.ban_list(DAY, fetcher=Fetcher({_archive(DAY): SAMPLE}))
    assert fo_ban.is_banned("RBLBANK", lists=lists) is True
    assert fo_ban.is_banned("rblbank", lists=lists) is True
    assert fo_ban.is_banned("RELIANCE", lists=lists) is False
    for never in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
                  "NSE_INDEX|Nifty 50", "NSE_EQ|INE976G01028", "SIM|RBLBANK"):
        assert fo_ban.is_banned(never, lists=fo_ban.BanList(None, frozenset(), False, "x")) is False, never


def test_an_fo_key_resolves_to_its_underlying(env):
    from backend.data import store

    store.write(
        "INSERT INTO instruments (instrument_key, trading_symbol, instrument_type, lot_size, "
        "underlying_key, underlying_symbol) VALUES (?,?,?,?,?,?), (?,?,?,?,?,?)",
        ["NSE_FO|11111", "RBLBANK25JANFUT", "FUT", 2500, "NSE_EQ|INE976G01028", "RBLBANK",
         "NSE_FO|22222", "NIFTY25JAN23000CE", "CE", 75, "NSE_INDEX|Nifty 50", "NIFTY"],
    )
    lists = fo_ban.ban_list(DAY, fetcher=Fetcher({_archive(DAY): SAMPLE}))
    assert fo_ban.is_banned("NSE_FO|11111", lists=lists) is True
    assert fo_ban.is_banned("NSE_FO|22222", lists=lists) is False, "index options are never banned"
    assert fo_ban.is_banned("NSE_FO|99999", lists=lists) is None, "an unknown F&O key is unknown"


def test_is_banned_fetches_the_days_list_when_not_given(env, monkeypatch):
    fetch = Fetcher({_archive(DAY): SAMPLE})
    monkeypatch.setattr(fo_ban, "_default_fetcher", fetch)
    assert fo_ban.is_banned("ANGELONE", DAY) is True
    assert fo_ban.is_banned("TCS", DAY) is False
    assert fetch.calls == [_archive(DAY)], "the second answer came from the cache"
