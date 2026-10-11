"""GameEngine: bars → indicators → regime → strategies (+ BASELINE) → simulated trades.

One engine serves RESEARCH replay (bar mode from klines, or tick mode from recorded
Parquet) and live SHADOW (tick mode fed by MarketDataHub). Every strategy runs on every
subscribed symbol; whether a game may trade for real is decided by the lifecycle, not here.
"""

from __future__ import annotations

import zlib
from collections.abc import Callable
from dataclasses import dataclass

import structlog

from advantage_player.config.settings import AppConfig, GameCfg
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.events import AggTrade, BookTicker, Depth, MarketEvent, MarkPrice
from advantage_player.marketdata.state import MarketState
from advantage_player.sim.fills import Costs, SimTrade, TradeResult
from advantage_player.strategies.a_trend import TrendPullback
from advantage_player.strategies.b_ofi import OrderFlowImbalance
from advantage_player.strategies.base import Signal, Strategy, StrategyContext, game_id
from advantage_player.strategies.baseline import RandomBaseline
from advantage_player.strategies.c_range import RangeFade
from advantage_player.strategies.indicators import Regime, SeriesState

log = structlog.get_logger(__name__)

STRATEGY_CLASSES: dict[str, Callable[[GameCfg], Strategy]] = {
    "A_trend_pullback": TrendPullback,
    "B_order_flow": OrderFlowImbalance,
    "C_range_fade": RangeFade,
}


@dataclass(frozen=True)
class SignalEvent:
    game_id: str
    symbol: str
    tf: str
    regime: Regime
    signal: Signal
    ts_ms: int
    baseline: bool


class ImbalanceTracker:
    """Since when has top-5 imbalance continuously exceeded the threshold, per side."""

    def __init__(self, threshold: float) -> None:
        self.thr = threshold
        self.since: dict[str, dict[int, int]] = {}

    def update(self, d: Depth) -> None:
        imb = d.imbalance(5)
        cur = self.since.setdefault(d.symbol, {})
        for side, active in ((1, imb > self.thr), (-1, imb < 1 - self.thr)):
            if active:
                cur.setdefault(side, d.event_ms)
            else:
                cur.pop(side, None)


class GameEngine:
    def __init__(
        self,
        cfg: AppConfig,
        costs: Callable[[str], Costs],
        tick_size: Callable[[str], float] = lambda _s: 0.0,
        *,
        bar_mode: bool = False,
        seed: int = 0,
        state: MarketState | None = None,
    ) -> None:
        self.cfg = cfg
        self.costs = costs
        self.tick_size = tick_size
        self.bar_mode = bar_mode
        self.seed = seed
        self.state = state or MarketState()
        self.strategies: list[tuple[str, Strategy]] = [
            (name, STRATEGY_CLASSES[name](g))
            for name, g in cfg.games.items()
            if g.enabled and name in STRATEGY_CLASSES
        ]
        b_cfg = cfg.games.get("B_order_flow")
        self.imbalance = ImbalanceTracker(b_cfg.param("imbalance", 0.7) if b_cfg else 0.7)
        self.series: dict[tuple[str, str], SeriesState] = {}
        self.baselines: dict[tuple[str, str, str], RandomBaseline] = {}
        self.open: dict[str, tuple[SimTrade, str]] = {}  # game_id → (trade, tf)
        self.result_hooks: list[Callable[[TradeResult], None]] = []
        self.signal_hooks: list[Callable[[SignalEvent], None]] = []
        self.unfilled: dict[str, int] = {}  # maker entries cancelled, per game
        self._next_funding: dict[str, tuple[int, float]] = {}

    @property
    def timeframes(self) -> set[str]:
        return {tf for _, s in self.strategies for tf in s.cfg.timeframes}

    # ---- events (tick mode) ----------------------------------------------------------------
    def on_event(self, ev: MarketEvent) -> None:
        if isinstance(ev, Depth):
            self.imbalance.update(ev)
        elif isinstance(ev, AggTrade):
            self._drive(ev.symbol, ev.price, ev.price, ev.trade_ms, ev.price)
        elif isinstance(ev, MarkPrice):
            self._funding(ev)
        elif isinstance(ev, BookTicker):
            pass

    def _funding(self, ev: MarkPrice) -> None:
        prev = self._next_funding.get(ev.symbol)
        if prev and ev.next_funding_ms > prev[0] and ev.event_ms >= prev[0]:
            for trade, _ in self.open.values():
                if trade.symbol == ev.symbol:
                    trade.on_funding(prev[1], ev.mark)
        self._next_funding[ev.symbol] = (ev.next_funding_ms, ev.funding_rate)

    def _drive(self, symbol: str, lo: float, hi: float, ts: int, ref: float) -> None:
        book = self.state.symbols.get(symbol)
        bt = book.book if book else None
        for gid, (trade, _tf) in list(self.open.items()):
            if trade.symbol != symbol:
                continue
            if trade.state == "pending":
                trade.try_enter(
                    lo,
                    hi,
                    ts,
                    bt.bid if bt and not self.bar_mode else None,
                    bt.ask if bt and not self.bar_mode else None,
                    ref,
                )
            trade.update(lo, hi, ts, first=ref)
            self._reap(gid, trade)

    def _reap(self, gid: str, trade: SimTrade) -> None:
        if trade.state == "closed":
            del self.open[gid]
            res = trade.result()
            for h in self.result_hooks:
                h(res)
        elif trade.state == "cancelled":
            del self.open[gid]
            self.unfilled[gid] = self.unfilled.get(gid, 0) + 1

    # ---- bars ------------------------------------------------------------------------------
    def on_bar(self, bar: Bar) -> None:
        tf = _tf_name(bar.tf_s)
        if self.bar_mode:  # bar replay: this bar is the price path for open trades
            self._drive(bar.symbol, bar.low, bar.high, bar.close_ms, bar.open)
        for gid, (trade, ttf) in list(self.open.items()):
            if trade.symbol == bar.symbol and ttf == tf:
                trade.on_bar_close(bar.close, bar.close_ms)
                self._reap(gid, trade)
        key = (bar.symbol, tf)
        ser = self.series.get(key)
        if ser is None:
            ser = self.series[key] = SeriesState(bar.symbol, bar.tf_s, self.cfg.regime)
        snap = ser.update(bar)
        if snap.regime in (Regime.WARMUP, Regime.NONE):
            return
        if snap.regime is Regime.HIGH_VOL and not self.cfg.regime.allow_high_vol:
            return
        st = self.state.symbols.get(bar.symbol)
        ctx = StrategyContext(
            symbol=bar.symbol,
            tf=tf,
            bars=list(ser.bars)[-10:],
            snap=snap,
            book=st.book if st else None,
            depth=st.depth if st else None,
            tick_size=self.tick_size(bar.symbol),
            imbalance_since_ms=self.imbalance.since.get(bar.symbol, {}),
            now_ms=bar.close_ms,
        )
        for name, strat in self.strategies:
            if tf not in strat.cfg.timeframes or snap.regime.value not in strat.cfg.regimes:
                continue
            sig = strat.on_bar(ctx)
            base = self._baseline(name, strat, bar.symbol, tf)
            base.observe(ctx, sig)
            bsig = base.on_bar(ctx)
            if sig is not None:
                self._open(game_id(strat.name, bar.symbol, tf, snap.regime), sig, ctx, False)
            if bsig is not None:
                self._open(game_id(base.name, bar.symbol, tf, snap.regime), bsig, ctx, True)

    def _baseline(self, name: str, strat: Strategy, symbol: str, tf: str) -> RandomBaseline:
        key = (name, symbol, tf)
        b = self.baselines.get(key)
        if b is None:
            seed = zlib.crc32(f"{self.seed}|{'|'.join(key)}".encode())
            b = self.baselines[key] = RandomBaseline(strat, seed=seed)
        return b

    def _open(self, gid: str, sig: Signal, ctx: StrategyContext, baseline: bool) -> None:
        ev = SignalEvent(gid, ctx.symbol, ctx.tf, ctx.snap.regime, sig, ctx.now_ms, baseline)
        for h in self.signal_hooks:
            h(ev)
        if gid in self.open:
            return  # one simulated position per game
        self.open[gid] = (
            SimTrade(gid, ctx.symbol, sig, ctx.now_ms, self.costs(ctx.symbol)),
            ctx.tf,
        )

    def close_all(self, price_of: Callable[[str], float], ts: int) -> None:
        for gid, (trade, _) in list(self.open.items()):
            trade.force_exit(price_of(trade.symbol), ts)
            self._reap(gid, trade)


def _tf_name(tf_s: int) -> str:
    if tf_s % 60 == 0:
        return f"{tf_s // 60}m"
    return f"{tf_s}s"
