"""Indian trading cost model — brokerage and every statutory charge.

Costs are the difference between a backtest and a fantasy. On 5-minute intraday
strategies they routinely exceed the gross edge, so modelling them as a flat
"₹20 per trade" understates the real drag by a wide margin, and always in the
flattering direction.

Everything is Decimal. Charges are computed per leg-side, then summed, matching
how a contract note is actually assembled.

Rates current as of August 2026
-------------------------------
Union Budget 2026 raised derivatives STT effective 1 April 2026:

    futures         0.02%  -> 0.05%   (sell side)
    options premium 0.10%  -> 0.15%   (sell side)
    options exercise 0.125% -> 0.15%  (of intrinsic, paid by the buyer)

Equity delivery (0.1% both sides) and intraday (0.025% sell) were unchanged.
These rates live in one place so a future revision is a one-line edit rather
than a hunt through the codebase.

Sources: Union Budget 2026; NSE circulars; Upstox published charge sheet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from backend.core.interfaces import Side

ZERO = Decimal("0")
PAISE = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


class Segment(str, Enum):
    EQUITY_DELIVERY = "equity_delivery"
    EQUITY_INTRADAY = "equity_intraday"
    FUTURES = "futures"
    OPTIONS = "options"
    COMMODITY_FUTURES = "commodity_futures"
    COMMODITY_OPTIONS = "commodity_options"


@dataclass(frozen=True, slots=True)
class ChargeSchedule:
    """Every rate for one segment. Percentages are expressed as percent, not bps."""

    brokerage_flat: Decimal = Decimal("20")
    brokerage_pct: Decimal | None = None      # applied to turnover
    brokerage_is_lower_of: bool = True        # min(flat, pct) when both are set

    stt_pct: Decimal = ZERO
    stt_on_sell_only: bool = True

    exchange_pct: Decimal = ZERO
    sebi_pct: Decimal = Decimal("0.0001")     # Rs 10 per crore
    ipft_pct: Decimal = ZERO
    stamp_pct: Decimal = ZERO                 # buy side only
    gst_pct: Decimal = Decimal("18")

    dp_charge: Decimal = ZERO                 # per scrip per day, delivery sell


# Rates as of August 2026. Percentages of turnover unless noted.
SCHEDULES: dict[Segment, ChargeSchedule] = {
    Segment.EQUITY_DELIVERY: ChargeSchedule(
        brokerage_flat=Decimal("20"),
        stt_pct=Decimal("0.1"), stt_on_sell_only=False,   # both sides
        exchange_pct=Decimal("0.00297"),
        ipft_pct=Decimal("0.0001"),
        stamp_pct=Decimal("0.015"),
        dp_charge=Decimal("20"),
    ),
    Segment.EQUITY_INTRADAY: ChargeSchedule(
        brokerage_flat=Decimal("20"), brokerage_pct=Decimal("0.1"),
        stt_pct=Decimal("0.025"), stt_on_sell_only=True,
        exchange_pct=Decimal("0.00297"),
        ipft_pct=Decimal("0.0001"),
        stamp_pct=Decimal("0.003"),
    ),
    Segment.FUTURES: ChargeSchedule(
        brokerage_flat=Decimal("20"), brokerage_pct=Decimal("0.05"),
        stt_pct=Decimal("0.05"), stt_on_sell_only=True,   # hiked 01-Apr-2026
        exchange_pct=Decimal("0.00173"),
        ipft_pct=Decimal("0.0001"),
        stamp_pct=Decimal("0.002"),
    ),
    Segment.OPTIONS: ChargeSchedule(
        brokerage_flat=Decimal("20"), brokerage_pct=None,  # flat, no percentage
        stt_pct=Decimal("0.15"), stt_on_sell_only=True,    # hiked 01-Apr-2026
        exchange_pct=Decimal("0.03503"),                   # on premium
        ipft_pct=Decimal("0.0005"),
        stamp_pct=Decimal("0.003"),
    ),
    Segment.COMMODITY_FUTURES: ChargeSchedule(
        brokerage_flat=Decimal("20"), brokerage_pct=Decimal("0.05"),
        stt_pct=Decimal("0.01"), stt_on_sell_only=True,    # CTT, non-agri
        exchange_pct=Decimal("0.0026"),
        stamp_pct=Decimal("0.002"),
    ),
    Segment.COMMODITY_OPTIONS: ChargeSchedule(
        brokerage_flat=Decimal("20"),
        stt_pct=Decimal("0.05"), stt_on_sell_only=True,
        exchange_pct=Decimal("0.05"),
        stamp_pct=Decimal("0.003"),
    ),
}


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """An itemized contract note for one order."""

    turnover: Decimal
    brokerage: Decimal = ZERO
    stt: Decimal = ZERO
    exchange: Decimal = ZERO
    sebi: Decimal = ZERO
    ipft: Decimal = ZERO
    gst: Decimal = ZERO
    stamp: Decimal = ZERO
    dp: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return _round(
            self.brokerage + self.stt + self.exchange
            + self.sebi + self.ipft + self.gst + self.stamp + self.dp
        )

    @property
    def bps_of_turnover(self) -> float:
        if self.turnover == 0:
            return 0.0
        return float(self.total / self.turnover * Decimal(10_000))

    def as_dict(self) -> dict[str, float]:
        return {
            "turnover": float(self.turnover),
            "brokerage": float(self.brokerage),
            "stt": float(self.stt),
            "exchange": float(self.exchange),
            "sebi": float(self.sebi),
            "ipft": float(self.ipft),
            "gst": float(self.gst),
            "stamp": float(self.stamp),
            "dp": float(self.dp),
            "total": float(self.total),
            "bps": round(self.bps_of_turnover, 3),
        }

    def __str__(self) -> str:
        return (
            f"turnover {self.turnover:,.2f} -> total {self.total:,.2f} "
            f"({self.bps_of_turnover:.2f} bps) "
            f"[brokerage {self.brokerage:.2f}, STT {self.stt:.2f}, "
            f"exch {self.exchange:.2f}, GST {self.gst:.2f}, stamp {self.stamp:.2f}]"
        )


def compute_costs(
    segment: Segment,
    side: Side,
    quantity: int,
    price: Decimal,
    schedule: ChargeSchedule | None = None,
) -> CostBreakdown:
    """Charges for one executed order.

    For options, `price` is the premium — turnover is premium x quantity, never
    the notional. Charging option costs on notional would overstate them by
    orders of magnitude, which is the mirror image of the usual mistake.
    """
    sched = schedule or SCHEDULES[segment]
    quantity = abs(quantity)
    turnover = _round(price * Decimal(quantity))

    if turnover == 0:
        return CostBreakdown(turnover=ZERO)

    # --- brokerage --------------------------------------------------------
    if sched.brokerage_pct is not None:
        pct_value = turnover * sched.brokerage_pct / Decimal(100)
        brokerage = min(sched.brokerage_flat, pct_value) if sched.brokerage_is_lower_of else pct_value
    else:
        brokerage = sched.brokerage_flat
    brokerage = _round(brokerage)

    # --- STT / CTT --------------------------------------------------------
    charge_stt = (side is Side.SELL) if sched.stt_on_sell_only else True
    stt = _round(turnover * sched.stt_pct / Decimal(100)) if charge_stt else ZERO

    # --- exchange, SEBI, IPFT --------------------------------------------
    exchange = _round(turnover * sched.exchange_pct / Decimal(100))
    sebi = _round(turnover * sched.sebi_pct / Decimal(100))
    ipft = _round(turnover * sched.ipft_pct / Decimal(100))

    # --- GST: on brokerage + exchange + SEBI + IPFT, never on STT or stamp
    gst = _round((brokerage + exchange + sebi + ipft) * sched.gst_pct / Decimal(100))

    # --- stamp duty: buy side only ----------------------------------------
    stamp = _round(turnover * sched.stamp_pct / Decimal(100)) if side is Side.BUY else ZERO

    # --- DP: delivery sell only -------------------------------------------
    dp = sched.dp_charge if (side is Side.SELL and sched.dp_charge > 0) else ZERO

    return CostBreakdown(
        turnover=turnover, brokerage=brokerage, stt=stt, exchange=exchange,
        sebi=sebi, ipft=ipft, gst=gst, stamp=stamp, dp=dp,
    )


def round_trip_cost(
    segment: Segment, quantity: int, entry_price: Decimal, exit_price: Decimal | None = None
) -> CostBreakdown:
    """Total cost of entering and exiting. Useful for a break-even estimate."""
    exit_price = exit_price if exit_price is not None else entry_price
    buy = compute_costs(segment, Side.BUY, quantity, entry_price)
    sell = compute_costs(segment, Side.SELL, quantity, exit_price)
    return CostBreakdown(
        turnover=buy.turnover + sell.turnover,
        brokerage=buy.brokerage + sell.brokerage,
        stt=buy.stt + sell.stt,
        exchange=buy.exchange + sell.exchange,
        sebi=buy.sebi + sell.sebi,
        ipft=buy.ipft + sell.ipft,
        gst=buy.gst + sell.gst,
        stamp=buy.stamp + sell.stamp,
        dp=buy.dp + sell.dp,
    )


def breakeven_move(segment: Segment, quantity: int, price: Decimal) -> Decimal:
    """Price move needed just to cover a round trip. The number worth knowing
    before deciding whether a strategy has any chance on a given timeframe."""
    cost = round_trip_cost(segment, quantity, price)
    return _round(cost.total / Decimal(quantity)) if quantity else ZERO


def segment_for(strategy_segment: str, is_intraday: bool, leg_kind: str) -> Segment:
    """Map a strategy document's instrument/leg description to a charge segment."""
    kind = leg_kind.lower()
    if kind == "option":
        return Segment.COMMODITY_OPTIONS if strategy_segment == "commodity" else Segment.OPTIONS
    if kind == "future":
        return Segment.COMMODITY_FUTURES if strategy_segment == "commodity" else Segment.FUTURES
    return Segment.EQUITY_INTRADAY if is_intraday else Segment.EQUITY_DELIVERY
