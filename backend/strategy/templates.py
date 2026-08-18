"""Shipped strategy templates — starting points, not black boxes.

Each is an ordinary strategy document. Open one in the builder, change what you
like, save it as your own. Nothing here is hidden or special-cased in the engine.

These are deliberately simple and deliberately not tuned. A template that
arrived pre-optimized would be a curve fit shipped as a default, and you would
have no way to tell which parts were real.
"""

from __future__ import annotations

from datetime import time
from typing import Callable

from backend.strategy.schema import Strategy

INTRADAY_SESSION = {"start": time(9, 20), "end": time(15, 10), "square_off": time(15, 15)}


# ---------------------------------------------------------------------------
# Directional
# ---------------------------------------------------------------------------

def ema_crossover(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    return Strategy.model_validate({
        "name": "EMA Crossover",
        "description": "Long when EMA(9) crosses above EMA(20), exit on the reverse cross.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "ema_fast", "type": "EMA", "params": {"period": 9}},
            {"id": "ema_slow", "type": "EMA", "params": {"period": 20}},
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "ema_fast", "cmp": "crosses_above", "right": "ema_slow"},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "ema_fast", "cmp": "crosses_below", "right": "ema_slow"},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 1.5, "ref": "atr"},
            "target": {"type": "atr_multiple", "value": 3.0, "ref": "atr"},
        }],
        "risk": {"max_concurrent_positions": 1, "max_trades_per_day": 4,
                 "re_entry": {"enabled": True, "max": 3, "cooldown_bars": 2}},
    })


def macd_crossover(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "15m") -> Strategy:
    return Strategy.model_validate({
        "name": "MACD Crossover",
        "description": "Long when the MACD line crosses above its signal line while above zero.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "macd", "type": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "macd.macd", "cmp": "crosses_above", "right": "macd.signal"},
            {"left": "macd.macd", "cmp": ">", "right": 0},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "macd.macd", "cmp": "crosses_below", "right": "macd.signal"},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 2.0, "ref": "atr"},
        }],
        "risk": {"max_trades_per_day": 3, "re_entry": {"enabled": True, "max": 2, "cooldown_bars": 3}},
    })


def cpr_breakout(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    """Uses yesterday's CPR, injected by the engine as session context."""
    return Strategy.model_validate({
        "name": "CPR Breakout",
        "description": "Long on a close above the previous day's TC, with volume confirmation.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
            {"id": "vol_avg", "type": "VOLUMESMA", "params": {"period": 20}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "close", "cmp": "crosses_above", "right": "tc"},
            {"left": "volume", "cmp": ">", "right": "vol_avg"},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "close", "cmp": "crosses_below", "right": "pivot"},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 1.5, "ref": "atr"},
            "trail": {"type": "percent", "value": 0.5, "activate_at": 0.5},
        }],
        "risk": {"max_trades_per_day": 2},
    })


def volume_spike_breakout(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    return Strategy.model_validate({
        "name": "Volume Spike Breakout",
        "description": "Long on a 20-bar high accompanied by at least twice average volume.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "dc", "type": "DONCHIAN", "params": {"period": 20}},
            {"id": "vol_ratio", "type": "VOLUMERATIO", "params": {"period": 20}},
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "close", "cmp": ">=", "right": "dc.upper"},
            {"left": "vol_ratio", "cmp": ">", "right": 2.0},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "close", "cmp": "<", "right": "dc.middle"},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 2.0, "ref": "atr"},
            "target": {"type": "percent", "value": 1.5},
        }],
        "risk": {"max_trades_per_day": 3},
    })


def rsi_mean_reversion(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "15m") -> Strategy:
    return Strategy.model_validate({
        "name": "RSI Mean Reversion",
        "description": "Buy oversold (RSI crosses back above 30), exit at RSI 60.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "rsi", "type": "RSI", "params": {"period": 14}},
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "rsi", "cmp": "crosses_above", "right": 30},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "rsi", "cmp": ">", "right": 60},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 2.0, "ref": "atr"},
        }],
        "risk": {"max_trades_per_day": 3, "re_entry": {"enabled": True, "max": 2, "cooldown_bars": 4}},
    })


def bollinger_breakout(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    return Strategy.model_validate({
        "name": "Bollinger Breakout",
        "description": "Long on a close above the upper band, exit back at the middle band.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "bb", "type": "BOLLINGER", "params": {"period": 20, "std_dev": 2.0}},
            {"id": "atr", "type": "ATR", "params": {"period": 14}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "close", "cmp": "crosses_above", "right": "bb.upper"},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "close", "cmp": "crosses_below", "right": "bb.middle"},
        ]},
        "legs": [{
            "id": "leg1", "action": "BUY", "kind": "equity", "lots": 1,
            "stop_loss": {"type": "atr_multiple", "value": 1.5, "ref": "atr"},
            "trail": {"type": "percent", "value": 0.4, "activate_at": 0.6},
        }],
        "risk": {"max_trades_per_day": 4},
    })


# ---------------------------------------------------------------------------
# Options structures
# ---------------------------------------------------------------------------

def short_straddle(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    return Strategy.model_validate({
        "name": "Short Straddle",
        "description": "Sell ATM call and put at 09:30, square off at 15:10. Premium decay play.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": {"start": time(9, 30), "end": time(15, 5), "square_off": time(15, 10)},
        "indicators": [{"id": "atr", "type": "ATR", "params": {"period": 14}}],
        "entry": {"op": "AND", "conditions": [
            {"left": "close", "cmp": ">", "right": 0},  # always true; timing does the work
        ]},
        "legs": [
            {"id": "call", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "ATM+0", "expiry": "current_weekly"},
             "stop_loss": {"type": "percent", "value": 30}},
            {"id": "put", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "ATM+0", "expiry": "current_weekly"},
             "stop_loss": {"type": "percent", "value": 30}},
        ],
        "risk": {"max_trades_per_day": 1, "max_concurrent_positions": 2},
    })


def long_straddle(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "5m") -> Strategy:
    return Strategy.model_validate({
        "name": "Long Straddle",
        "description": "Buy ATM call and put — a volatility expansion bet, not a directional one.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": {"start": time(9, 20), "end": time(15, 5), "square_off": time(15, 10)},
        "indicators": [{"id": "bb", "type": "BOLLINGER", "params": {"period": 20}}],
        "entry": {"op": "AND", "conditions": [
            {"left": "bb.bandwidth", "cmp": "<", "right": 0.004},  # squeeze
        ]},
        "legs": [
            {"id": "call", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "ATM+0", "expiry": "current_weekly"},
             "target": {"type": "percent", "value": 40}},
            {"id": "put", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "ATM+0", "expiry": "current_weekly"},
             "target": {"type": "percent", "value": 40}},
        ],
        "risk": {"max_trades_per_day": 1, "max_concurrent_positions": 2},
    })


def bull_call_spread(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "15m") -> Strategy:
    return Strategy.model_validate({
        "name": "Bull Call Spread",
        "description": "Buy ATM call, sell OTM call. Defined risk, capped reward, on trend.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "ema_fast", "type": "EMA", "params": {"period": 9}},
            {"id": "ema_slow", "type": "EMA", "params": {"period": 21}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "ema_fast", "cmp": "crosses_above", "right": "ema_slow"},
        ]},
        "legs": [
            {"id": "long_call", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "ATM+0", "expiry": "current_weekly"}},
            {"id": "short_call", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "OTM+2", "expiry": "current_weekly"}},
        ],
        "risk": {"max_trades_per_day": 2, "max_concurrent_positions": 2},
    })


def bear_put_spread(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "15m") -> Strategy:
    return Strategy.model_validate({
        "name": "Bear Put Spread",
        "description": "Buy ATM put, sell OTM put. Defined risk on a downtrend.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": INTRADAY_SESSION,
        "indicators": [
            {"id": "ema_fast", "type": "EMA", "params": {"period": 9}},
            {"id": "ema_slow", "type": "EMA", "params": {"period": 21}},
        ],
        "entry": {"op": "AND", "conditions": [
            {"left": "ema_fast", "cmp": "crosses_below", "right": "ema_slow"},
        ]},
        "legs": [
            {"id": "long_put", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "ATM+0", "expiry": "current_weekly"}},
            {"id": "short_put", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "OTM-2", "expiry": "current_weekly"}},
        ],
        "risk": {"max_trades_per_day": 2, "max_concurrent_positions": 2},
    })


def iron_condor(symbol: str = "NSE_INDEX|Nifty 50", timeframe: str = "15m") -> Strategy:
    return Strategy.model_validate({
        "name": "Iron Condor",
        "description": "Sell an OTM strangle, buy wings for protection. Range-bound premium capture.",
        "instrument": {"symbol": symbol, "segment": "index"},
        "timeframe": timeframe,
        "session": {"start": time(9, 30), "end": time(15, 5), "square_off": time(15, 10)},
        "indicators": [{"id": "bb", "type": "BOLLINGER", "params": {"period": 20}}],
        "entry": {"op": "AND", "conditions": [
            {"left": "bb.bandwidth", "cmp": "<", "right": 0.006},  # quiet market
        ]},
        "legs": [
            {"id": "short_call", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "OTM+2", "expiry": "current_weekly"}},
            {"id": "long_call", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "OTM+4", "expiry": "current_weekly"}},
            {"id": "short_put", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "OTM-2", "expiry": "current_weekly"}},
            {"id": "long_put", "action": "BUY", "kind": "option", "lots": 1,
             "option": {"right": "PE", "strike_rule": "OTM-4", "expiry": "current_weekly"}},
        ],
        "risk": {"max_trades_per_day": 1, "max_concurrent_positions": 4},
    })


def covered_call(symbol: str = "NSE_EQ|INE002A01018", timeframe: str = "1d") -> Strategy:
    return Strategy.model_validate({
        "name": "Covered Call",
        "description": "Hold the underlying, sell an OTM call against it for yield.",
        "instrument": {"symbol": symbol, "segment": "equity"},
        "timeframe": timeframe,
        "session": {"start": time(9, 20), "end": time(15, 20), "square_off": None},
        "indicators": [{"id": "rsi", "type": "RSI", "params": {"period": 14}}],
        "entry": {"op": "AND", "conditions": [
            {"left": "rsi", "cmp": ">", "right": 55},
        ]},
        "exit": {"op": "OR", "conditions": [
            {"left": "rsi", "cmp": "<", "right": 40},
        ]},
        "legs": [
            {"id": "stock", "action": "BUY", "kind": "equity", "lots": 1},
            {"id": "short_call", "action": "SELL", "kind": "option", "lots": 1,
             "option": {"right": "CE", "strike_rule": "OTM+3", "expiry": "current_monthly"}},
        ],
        "risk": {"max_trades_per_day": 1, "max_concurrent_positions": 2},
    })


TEMPLATES: dict[str, Callable[..., Strategy]] = {
    "ema_crossover": ema_crossover,
    "macd_crossover": macd_crossover,
    "cpr_breakout": cpr_breakout,
    "volume_spike_breakout": volume_spike_breakout,
    "rsi_mean_reversion": rsi_mean_reversion,
    "bollinger_breakout": bollinger_breakout,
    "short_straddle": short_straddle,
    "long_straddle": long_straddle,
    "bull_call_spread": bull_call_spread,
    "bear_put_spread": bear_put_spread,
    "iron_condor": iron_condor,
    "covered_call": covered_call,
}

DIRECTIONAL = [
    "ema_crossover", "macd_crossover", "cpr_breakout",
    "volume_spike_breakout", "rsi_mean_reversion", "bollinger_breakout",
]
OPTIONS = [
    "short_straddle", "long_straddle", "bull_call_spread",
    "bear_put_spread", "iron_condor", "covered_call",
]


def load(name: str, **kwargs) -> Strategy:
    if name not in TEMPLATES:
        raise KeyError(f"Unknown template {name!r}. Available: {', '.join(sorted(TEMPLATES))}")
    return TEMPLATES[name](**kwargs)


def list_templates() -> list[dict]:
    """Template catalogue for the UI's "start from a template" picker."""
    out = []
    for name, factory in TEMPLATES.items():
        strategy = factory()
        out.append({
            "key": name,
            "name": strategy.name,
            "description": strategy.description,
            "category": "options" if name in OPTIONS else "directional",
            "timeframe": strategy.timeframe,
            "legs": len(strategy.legs),
            "indicators": [i.type for i in strategy.indicators],
        })
    return out
