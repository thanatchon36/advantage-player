"""Conservative fill simulator shared by RESEARCH replay and SHADOW (SPEC §5).

Rules:
- Limit (maker) orders fill only when price trades THROUGH the level (strictly beyond it).
- Market orders pay half-spread + modeled impact (slippage_bps) against us.
- Stops (STOP_MARKET) fill at the worse of stop and observed price, minus slippage.
- TAKE_PROFIT_MARKET fills at target minus slippage; maker TPs need a trade through.
- When one price update could hit both stop and target (bar replay), the stop wins.
- Funding is charged while open; all results are R-multiples NET of fees/slippage/funding,
  where 1R = planned loss at the initial stop from the planned entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from advantage_player.strategies.base import Signal

ExitReason = Literal["stop", "target", "time", "be_stop", "trail_stop", "forced"]


@dataclass(frozen=True)
class Costs:
    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    slippage_bps: float = 1.0
    spread_bps: float = 1.0  # used when no live book is available
    maker_timeout_ms: int = 5_000


@dataclass
class Leg:
    price: float  # actual fill
    ref: float  # intended level (stop / target / close); slippage = d × (ref − price)
    fraction: float
    fee_rate: float
    reason: str


@dataclass
class TradeResult:
    game_id: str
    symbol: str
    side: str
    tag: str
    signal_ms: int
    entry_ms: int
    exit_ms: int
    entry_ref: float
    entry_px: float
    stop_initial: float
    legs: list[Leg]
    r_net: float
    r_gross: float
    fee_r: float
    funding_r: float
    slippage_r: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    entry_maker: bool
    adverse_bps: float | None  # mid move against us shortly after a maker fill

    @property
    def hold_ms(self) -> int:
        return self.exit_ms - self.entry_ms


@dataclass
class SimTrade:
    game_id: str
    symbol: str
    signal: Signal
    signal_ms: int
    costs: Costs
    state: Literal["pending", "open", "closed", "cancelled"] = "pending"
    entry_px: float = 0.0
    entry_ms: int = 0
    entry_fee: float = 0.0
    stop: float = 0.0
    remaining: float = 1.0
    next_target: int = 0
    bars_held: int = 0
    extreme: float = 0.0
    funding_paid: float = 0.0  # per unit, in price terms
    legs: list[Leg] = field(default_factory=list)
    mae: float = 0.0
    mfe: float = 0.0
    adverse_bps: float | None = None
    exit_reason: str = ""
    exit_ms: int = 0
    stop_kind: str = "stop"

    def __post_init__(self) -> None:
        self.stop = self.signal.stop

    @property
    def d(self) -> int:
        return self.signal.direction

    @property
    def slip(self) -> float:
        return self.costs.slippage_bps / 1e4

    # ---- entry -----------------------------------------------------------------------------
    def try_enter(
        self, lo: float, hi: float, ts: int, bid: float | None, ask: float | None, ref: float
    ) -> None:
        """Called with the first price info after the signal (tick: lo=hi=trade price)."""
        if self.state != "pending":
            return
        sig = self.signal
        if sig.entry_type == "market":
            if bid is not None and ask is not None:
                px = ask if self.d > 0 else bid
            else:
                px = ref * (1 + self.d * self.costs.spread_bps / 2e4)
            self._open(px * (1 + self.d * self.slip), ts, self.costs.taker_fee)
            return
        if ts - self.signal_ms > self.costs.maker_timeout_ms:
            self.state = "cancelled"
            return
        limit = sig.entry_ref
        through = lo < limit if self.d > 0 else hi > limit
        if through:
            self._open(limit, ts, self.costs.maker_fee)

    def _open(self, px: float, ts: int, fee: float) -> None:
        self.state = "open"
        self.entry_px = px
        self.entry_ms = ts
        self.entry_fee = fee
        self.extreme = px

    # ---- lifecycle -------------------------------------------------------------------------
    def update(self, lo: float, hi: float, ts: int, first: float | None = None) -> None:
        """Price range observed since the last update (tick: lo == hi == first).

        `first` is the first price of the range (bar open); a range that opens beyond the
        stop is a gap and fills at that price, otherwise the stop fills at its level.
        """
        if self.state != "open":
            return
        risk = self.signal.risk_per_unit
        adverse = (self.entry_px - lo) if self.d > 0 else (hi - self.entry_px)
        favorable = (hi - self.entry_px) if self.d > 0 else (self.entry_px - lo)
        self.mae = max(self.mae, adverse / risk)
        self.mfe = max(self.mfe, favorable / risk)
        if (
            self.signal.entry_type == "maker"
            and self.adverse_bps is None
            and (ts - self.entry_ms >= 5_000)
        ):
            self.adverse_bps = self.mae * risk / self.entry_px * 1e4
        # stop first (conservative)
        stop_hit = lo <= self.stop if self.d > 0 else hi >= self.stop
        if stop_hit:
            f = lo if first is None else first  # no `first` → assume worst observed price
            fill = min(self.stop, f) if self.d > 0 else max(self.stop, hi if first is None else f)
            px = fill * (1 - self.d * self.slip)
            self._exit(px, self.stop, self.remaining, self.costs.taker_fee, self.stop_kind, ts)
            return
        targets = self.signal.targets
        while self.next_target < len(targets):
            t = targets[self.next_target]
            tp = self.signal.target_price(t)
            reached = (
                (hi > tp if t.maker else hi >= tp)
                if self.d > 0
                else (lo < tp if t.maker else lo <= tp)
            )
            if not reached:
                break
            frac = min(t.fraction, self.remaining)
            if t.maker:
                self._leg(tp, tp, frac, self.costs.maker_fee, "target")
            else:
                px = tp * (1 - self.d * self.slip)
                self._leg(px, tp, frac, self.costs.taker_fee, "target")
            self.next_target += 1
            if self.remaining <= 1e-12:
                self._close("target", ts)
                return
            if self.next_target == 1:
                self._after_first_target()
        # trailing
        self.extreme = max(self.extreme, hi) if self.d > 0 else min(self.extreme, lo)
        if self.next_target >= 1 and self.signal.trail_atr > 0 and self.signal.atr > 0:
            trail = self.extreme - self.d * self.signal.trail_atr * self.signal.atr
            if (trail - self.stop) * self.d > 0:  # SL only moves toward profit
                self.stop = trail
                self.stop_kind = "trail_stop"

    def _after_first_target(self) -> None:
        be = self.signal.move_stop_to_be_after_first_target
        if be and (self.entry_px - self.stop) * self.d > 0:
            self.stop = self.entry_px
            self.stop_kind = "be_stop"
        if self.signal.trail_atr > 0 and not self.signal.move_stop_to_be_after_first_target:
            self.stop_kind = "trail_stop"

    def on_bar_close(self, close: float, ts: int) -> None:
        if self.state != "open":
            return
        self.bars_held += 1
        n = self.signal.time_stop_bars
        if n and self.bars_held >= n:
            px = close * (1 - self.d * (self.costs.spread_bps / 2e4 + self.slip))
            self._exit(px, close, self.remaining, self.costs.taker_fee, "time", ts)

    def on_funding(self, rate: float, mark: float) -> None:
        """Longs pay positive funding, shorts receive it (per unit of remaining size)."""
        if self.state == "open":
            self.funding_paid += self.d * rate * mark * self.remaining

    def force_exit(self, px: float, ts: int, reason: str = "forced") -> None:
        if self.state == "open":
            self._exit(
                px * (1 - self.d * self.slip), px, self.remaining, self.costs.taker_fee, reason, ts
            )
        elif self.state == "pending":
            self.state = "cancelled"

    # ---- internals -------------------------------------------------------------------------
    def _leg(self, px: float, ref: float, frac: float, fee: float, reason: str) -> None:
        self.legs.append(Leg(px, ref, frac, fee, reason))
        self.remaining -= frac

    def _exit(self, px: float, ref: float, frac: float, fee: float, reason: str, ts: int) -> None:
        self._leg(px, ref, frac, fee, reason)
        self._close(reason, ts)

    def _close(self, reason: str, ts: int) -> None:
        self.state = "closed"
        self.exit_reason = reason
        self.exit_ms = ts

    def result(self) -> TradeResult:
        assert self.state == "closed"
        sig = self.signal
        risk = sig.risk_per_unit
        pnl = sum(self.d * (leg.price - self.entry_px) * leg.fraction for leg in self.legs)
        fees = self.entry_px * self.entry_fee + sum(
            leg.price * leg.fee_rate * leg.fraction for leg in self.legs
        )
        # slippage vs intended levels (positive = cost): entry vs plan, exits vs trigger level
        slip = self.d * (self.entry_px - sig.entry_ref) + sum(
            self.d * (leg.ref - leg.price) * leg.fraction for leg in self.legs
        )
        return TradeResult(
            game_id=self.game_id,
            symbol=self.symbol,
            side=sig.side,
            tag=sig.tag,
            signal_ms=self.signal_ms,
            entry_ms=self.entry_ms,
            exit_ms=self.exit_ms,
            entry_ref=sig.entry_ref,
            entry_px=self.entry_px,
            stop_initial=sig.stop,
            legs=list(self.legs),
            r_net=(pnl - fees - self.funding_paid) / risk,
            r_gross=(pnl + slip) / risk,
            fee_r=fees / risk,
            funding_r=self.funding_paid / risk,
            slippage_r=slip / risk,
            mae_r=self.mae,
            mfe_r=self.mfe,
            exit_reason=self.exit_reason,
            entry_maker=sig.entry_type == "maker",
            adverse_bps=self.adverse_bps,
        )
