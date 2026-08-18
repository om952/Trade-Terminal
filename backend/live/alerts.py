"""Telegram notifications.

One rule governs this whole module: an alert failure must never touch the trading
loop. Telegram being down, rate-limiting, or misconfigured is an inconvenience;
a session that dies holding an open position because a notification raised is a
real loss. Every send is wrapped, every failure is logged and swallowed.

Alerts are also deliberately terse. A notification you have to read carefully at
09:47 while something is going wrong is a notification that will be misread.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx

from backend.core.config import settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"


class Alerts:
    """Sends to Telegram if configured, no-ops otherwise."""

    def __init__(self, enabled: bool = True) -> None:
        token = settings.telegram_bot_token.get_secret_value().strip()
        self.token = token
        self.chat_id = settings.telegram_chat_id.strip()
        self.enabled = bool(enabled and token and self.chat_id)
        self.sent = 0
        self.failed = 0
        if enabled and not self.enabled:
            log.info("Telegram not configured — alerts disabled for this session")

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            resp = httpx.post(
                API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=5.0,
            )
            if resp.status_code != 200:
                self.failed += 1
                log.warning("Telegram rejected the alert: HTTP %s", resp.status_code)
                return False
            self.sent += 1
            return True
        except Exception as exc:
            # Deliberately broad: nothing this class can hit is worth propagating
            # into a loop that is holding a position.
            self.failed += 1
            log.warning("Telegram send failed: %s", exc)
            return False

    # -- shaped messages -----------------------------------------------------

    def session_start(self, name: str, symbol: str, timeframe: str, mode: str) -> None:
        self.send(f"▶️ <b>{mode.upper()}</b> started\n{name}\n{symbol} · {timeframe}")

    def entry(self, symbol: str, side: str, quantity: int, price: Decimal) -> None:
        self.send(f"🟢 <b>ENTRY</b> {side} {quantity} {symbol} @ {price:.2f}")

    def exit(self, symbol: str, quantity: int, price: Decimal, pnl: Decimal, reason: str) -> None:
        mark = "✅" if pnl >= 0 else "🔴"
        self.send(
            f"{mark} <b>EXIT</b> {quantity} {symbol} @ {price:.2f}\n"
            f"P&L {pnl:+.2f} · {reason}"
        )

    def blocked(self, reason: str, detail: str) -> None:
        self.send(f"⛔️ Entry blocked: {reason}\n{detail}")

    def error(self, message: str) -> None:
        self.send(f"⚠️ <b>ERROR</b>\n{message[:600]}")

    def session_end(self, name: str, trades: int, pnl: Decimal, reason: str) -> None:
        self.send(
            f"⏹ <b>Session ended</b> ({reason})\n{name}\n"
            f"{trades} trade(s) · net {pnl:+.2f}"
        )
