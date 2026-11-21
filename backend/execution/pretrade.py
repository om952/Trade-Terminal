"""Pre-trade checks in front of every broker (P5.2).

`GuardedBroker` wraps any `Broker` (the simulated one now, Upstox's in P5.1)
and refuses an order before it reaches the market when it breaks a limit. The
checks are the NSE pre-trade controls that apply to a self-built algo
(NSE/INVG/69255 para 11.1, mapped in docs/REGULATORY_CHECK.md section 6),
plus the account caps the operator sets:

* **No market orders.** Algo market orders are banned and rejected at the
  exchange; only LIMIT and SL orders pass.
* **Price band (fat finger).** A limit more than `price_band_pct` from the
  price it was set against is refused. For an SL order, the limit is checked
  against its trigger.
* **Freeze quantity.** An order above the exchange's freeze quantity is
  refused; it must be sliced first.
* **Order size and notional.** Caps per order, applied to orders that open or
  add exposure. Orders that reduce exposure are never refused by a cap, so a
  stop or a flatten always goes out.
* **F&O ban period.** No new position in a stock whose derivatives are in the
  ban period. When the day's ban list cannot be read, new F&O stock positions
  are refused (fail closed).
* **Runaway stop.** The guard trips, and refuses everything except orders that
  reduce exposure, after `max_orders_per_day` orders, after
  `max_consecutive_rejects` rejections in a row, or when the rate limiter
  would have to wait longer than `max_wait_s`.
* **Order rate.** At most `per_second` orders in any second (the exchange's
  threshold is 10, measured on the broker's clock), at least `min_spacing_s`
  apart, and per-minute and per-30-minute ceilings (Upstox: 500 and 2,000).
  Pacing sleeps; it is on for real-time feeds and off for replays, which
  compress time.

Every refusal is recorded in `rejections` with its reason.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from backend.core.config import IST
from backend.core.interfaces import Broker, Fill, Order, OrderType, Side

log = logging.getLogger(__name__)

HUNDRED = Decimal(100)


class Tripped(RuntimeError):
    """The runaway stop tripped: only orders that reduce exposure may go out."""


@dataclass(frozen=True)
class PreTradeLimits:
    max_order_quantity: int | None = None
    max_order_notional: Decimal | None = None
    price_band_pct: float = 5.0
    max_orders_per_day: int = 200
    max_consecutive_rejects: int = 5
    per_second: int = 5
    min_spacing_s: float = 0.15
    per_minute: int = 200
    per_30_minutes: int = 2000
    max_wait_s: float = 10.0

    @classmethod
    def from_settings(cls) -> PreTradeLimits:
        from backend.core.config import settings

        return cls(
            max_order_quantity=settings.max_order_quantity,
            max_order_notional=Decimal(str(settings.max_order_notional)) if settings.max_order_notional else None,
            price_band_pct=settings.order_price_band_pct,
            max_orders_per_day=settings.max_orders_per_day,
            max_consecutive_rejects=settings.max_consecutive_rejects,
            per_second=settings.order_rate_per_second,
            min_spacing_s=settings.order_min_spacing_ms / 1000,
            per_minute=settings.order_rate_per_minute,
            per_30_minutes=settings.order_rate_per_30_minutes,
        )


class RateLimiter:
    """Spacing and sliding-window ceilings. `acquire` returns the seconds it waited."""

    def __init__(self, limits: PreTradeLimits, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.limits, self._clock, self._sleep = limits, clock, sleep
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def wait_needed(self, now: float) -> float:
        lim, sent = self.limits, self._sent
        while sent and now - sent[0] >= 1800:
            sent.popleft()
        need = 0.0
        if sent:
            need = max(need, lim.min_spacing_s - (now - sent[-1]))
        for window, cap in ((1.0, lim.per_second), (60.0, lim.per_minute), (1800.0, lim.per_30_minutes)):
            inside = [t for t in sent if now - t < window]
            if len(inside) >= cap:
                need = max(need, window - (now - inside[-cap]))
        return max(0.0, need)

    def acquire(self) -> float:
        with self._lock:
            need = self.wait_needed(self._clock())
            if need > self.limits.max_wait_s:
                raise Tripped(f"the order rate would need a {need:.1f}s wait (more than "
                              f"{self.limits.max_wait_s:.0f}s): too many orders")
            if need > 0:
                self._sleep(need)
            self._sent.append(self._clock())
            return need


@dataclass
class Rejection:
    when: datetime
    client_tag: str
    symbol: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"when": self.when.isoformat(timespec="seconds"), "client_tag": self.client_tag,
                "symbol": self.symbol, "reason": self.reason}


@dataclass
class GuardedBroker:
    """Any `Broker`, behind the pre-trade checks. Other attributes pass through to it."""

    inner: Broker
    limits: PreTradeLimits = field(default_factory=PreTradeLimits)
    limiter: RateLimiter | None = None
    is_banned: Callable[[str], bool | None] | None = None
    freeze_quantity: Callable[[str], int | None] | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(IST))

    tripped: bool = field(default=False, init=False)
    trip_reason: str = field(default="", init=False)
    rejections: list[Rejection] = field(default_factory=list, init=False)
    _day: date | None = field(default=None, init=False)
    _orders_today: int = field(default=0, init=False)
    _consecutive_rejects: int = field(default=0, init=False)
    _now: datetime | None = field(default=None, init=False)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined here: the wrapped broker's extras
        # (reconcile_against, mark, restore, fills...).
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    # -- the checks ------------------------------------------------------------

    def check(self, order: Order, *, opening: bool) -> str | None:
        """Why this order must not go out, or None."""
        if self.tripped and opening:
            return f"runaway stop tripped earlier: {self.trip_reason}"
        if order.order_type not in (OrderType.LIMIT, OrderType.SL):
            return "market orders are not allowed for algo orders (NSE); send a LIMIT or SL order"
        if order.price is None or order.price <= 0:
            return "a limit price is required"
        if order.quantity <= 0:
            return "quantity must be positive"
        anchor = order.trigger_price if order.order_type is OrderType.SL else order.reference_price
        if anchor is not None and anchor > 0:
            away = abs(order.price - anchor) / anchor * HUNDRED
            if away > Decimal(str(self.limits.price_band_pct)):
                return (f"limit {order.price} is {away:.2f}% from {anchor}, outside the "
                        f"{self.limits.price_band_pct:g}% price band (fat-finger check)")
        if self.freeze_quantity is not None:
            freeze = self.freeze_quantity(order.symbol)
            if freeze and order.quantity > freeze:
                return f"quantity {order.quantity} is above the freeze quantity {freeze}; slice the order"
        if opening:
            if self.limits.max_order_quantity is not None and order.quantity > self.limits.max_order_quantity:
                return f"quantity {order.quantity} is above MAX_ORDER_QUANTITY {self.limits.max_order_quantity}"
            notional = order.price * Decimal(order.quantity)
            if self.limits.max_order_notional is not None and notional > self.limits.max_order_notional:
                return f"notional {notional:,.0f} is above MAX_ORDER_NOTIONAL {self.limits.max_order_notional:,.0f}"
            if self.is_banned is not None:
                banned = self.is_banned(order.symbol)
                if banned is None:
                    return "the F&O ban list for today could not be read; new F&O positions are refused"
                if banned:
                    return f"{order.symbol} is in the F&O ban period: no new positions"
        return None

    # -- the Broker protocol ---------------------------------------------------

    def place(self, order: Order, *, opening: bool | None = None, as_of: datetime | None = None) -> Fill | None:
        """Check, pace and send. `opening` says whether the order opens or adds exposure.

        Callers know (an entry opens; a stop, target, exit or flatten reduces).
        When it is not given, an order is treated as opening, the stricter case.
        `as_of` is the order's time when it is not now (a replay); the daily order
        count runs by it.
        """
        opens = True if opening is None else opening
        self._now = as_of
        self._roll_day()
        reason = self.check(order, opening=opens)
        if reason is None and opens and self._orders_today >= self.limits.max_orders_per_day:
            self._trip(f"{self._orders_today} orders today, the limit is {self.limits.max_orders_per_day}")
            reason = f"runaway stop: {self.trip_reason}"
        if reason is None and self.limiter is not None:
            try:
                self.limiter.acquire()
            except Tripped as exc:
                self._trip(str(exc))
                if opens:
                    reason = f"runaway stop: {exc}"
        if reason is not None:
            self._refuse(order, reason)
            return None

        self._orders_today += 1
        fill = self.inner.place(order)
        if fill is None:
            self._consecutive_rejects += 1
            if self._consecutive_rejects >= self.limits.max_consecutive_rejects:
                self._trip(f"{self._consecutive_rejects} orders in a row rejected by the broker")
        else:
            self._consecutive_rejects = 0
        return fill

    def cancel(self, client_tag: str) -> bool:
        return self.inner.cancel(client_tag)

    def positions(self):
        return self.inner.positions()

    def reconcile(self) -> bool:
        return self.inner.reconcile()

    # -- state -----------------------------------------------------------------

    def _time(self) -> datetime:
        return self._now if self._now is not None else self.clock()

    def _roll_day(self) -> None:
        today = self._time().date()
        if self._day != today:
            self._day, self._orders_today, self._consecutive_rejects = today, 0, 0

    def _trip(self, reason: str) -> None:
        if not self.tripped:
            log.error("Pre-trade runaway stop tripped: %s", reason)
        self.tripped, self.trip_reason = True, self.trip_reason or reason

    def _refuse(self, order: Order, reason: str) -> None:
        log.warning("Order %s refused before the broker: %s", order.client_tag, reason)
        self.rejections.append(Rejection(self._time(), order.client_tag, order.symbol, reason))
        self._consecutive_rejects += 1
        if self._consecutive_rejects >= self.limits.max_consecutive_rejects:
            self._trip(f"{self._consecutive_rejects} orders in a row refused")

    def status(self) -> dict[str, Any]:
        return {"tripped": self.tripped, "trip_reason": self.trip_reason, "orders_today": self._orders_today,
                "rejections": [r.as_dict() for r in self.rejections[-20:]]}


def side_opens(side: Side, current_quantity: int) -> bool:
    """Whether an order on `side` opens or adds to exposure given the current signed quantity."""
    if current_quantity == 0:
        return True
    return (current_quantity > 0) == (side is Side.BUY)
