"""Universe scanner ("table selection", SPEC §3). Runs every scan_interval_s.

Data sources (weights measured on DEMO): ticker/24hr all=40, premiumIndex all=10,
depth limit=50 → 2, klines limit<100 → 1, limit 168 → 2, openInterest → 1.
Prefilters by 24h quote volume before per-symbol calls to keep weight low.

Approximations (documented in PROGRESS.md):
- ATR at 5s/15s is scaled from 1m ATR by √(tf/60) until local bars exist.
- median spread uses streamed bookTicker samples if ≥ 1 h available, else the depth snapshot.
- OI change compares to the previous scan (DEMO doesn't serve /futures/data/openInterestHist).
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from advantage_player.config.settings import AppConfig
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.models import Fees, SymbolInfo
from advantage_player.exchange.rest import BinanceRest
from advantage_player.marketdata.bars import tf_seconds
from advantage_player.marketdata.state import MarketState
from advantage_player.scanner.metrics import (
    TableMetrics,
    atr,
    correlation_clusters,
    depth_within_bps,
    ineligibility,
    wick_anomaly,
)

log = structlog.get_logger(__name__)
DAY_MS = 86_400_000
FALLBACK_FEES = Fees("", maker=0.0002, taker=0.0005)  # conservative when no API keys


@dataclass
class ScanResult:
    ts_ms: int
    metrics: dict[str, TableMetrics]
    scores: dict[str, float]
    reasons: dict[str, list[str]]
    open: list[str]
    watch: list[str]
    clusters: list[set[str]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def cluster_of(self, symbol: str) -> int:
        for i, c in enumerate(self.clusters):
            if symbol in c:
                return i
        return -1


def universe(meta: ExchangeMetadata, cfg: AppConfig, now_ms: int) -> list[SymbolInfo]:
    u = cfg.universe
    out = []
    for s in meta.perpetuals(u.quote_asset):
        if s.status != "TRADING":
            continue
        if now_ms - s.onboard_ms < u.min_listing_days * DAY_MS:
            continue
        if s.delivery_ms < now_ms + 365 * DAY_MS:  # scheduled delivery / delist
            continue
        out.append(s)
    return out


def game_tf_pairs(cfg: AppConfig) -> list[tuple[str, str]]:
    return sorted({(tf, g.entry) for g in cfg.games.values() if g.enabled for tf in g.timeframes})


class TableScanner:
    def __init__(
        self, rest: BinanceRest, meta: ExchangeMetadata, state: MarketState, cfg: AppConfig
    ) -> None:
        self.rest = rest
        self.meta = meta
        self.state = state
        self.cfg = cfg
        self.last: ScanResult | None = None
        self._prev_oi: dict[str, float] = {}
        self._fee_failed: set[str] = set()
        self._sem = asyncio.Semaphore(8)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with self._sem:
            while self.rest.limiter.headroom() < self.cfg.discipline.min_rate_headroom:
                await asyncio.sleep(1.0)  # let the 1-minute weight window roll over
            return await self.rest.public("GET", path, params)

    async def scan(self, now_ms: int | None = None) -> ScanResult:
        now = now_ms or int(time.time() * 1000)
        u = self.cfg.universe
        uni = {s.symbol: s for s in universe(self.meta, self.cfg, now)}
        tickers, premium = await asyncio.gather(
            self._get("/fapi/v1/ticker/24hr"), self._get("/fapi/v1/premiumIndex")
        )
        qv = {t["symbol"]: float(t["quoteVolume"]) for t in tickers if t["symbol"] in uni}
        funding = {p["symbol"]: float(p.get("lastFundingRate") or 0) for p in premium}
        cands = [s for s, v in qv.items() if v >= u.min_quote_volume_usd]
        metrics = dict(
            zip(
                cands,
                await asyncio.gather(
                    *(self._symbol_metrics(uni[s], qv[s], funding.get(s, 0.0), now) for s in cands)
                ),
                strict=True,
            )
        )
        pairs = game_tf_pairs(self.cfg)
        scores = {s: m.score(pairs) for s, m in metrics.items()}
        # Per-symbol commissionRate is costly; fetch it only for the shortlist, then re-score.
        shortlist = sorted(metrics, key=lambda s: -scores[s])[: 2 * (u.open_tables + u.watchlist)]
        missing = [s for s in shortlist if s not in self.meta.fees and s not in self._fee_failed]
        if missing and self.rest.has_keys:
            await self.meta.refresh_fees(missing)
            self._fee_failed |= {s for s in missing if s not in self.meta.fees}
        for s in shortlist:
            f = self.meta.fees.get(s, FALLBACK_FEES)
            metrics[s].maker_fee_bps, metrics[s].taker_fee_bps = f.maker_bps, f.taker_bps
            scores[s] = metrics[s].score(pairs)
        reasons = {s: ineligibility(m, u, scores[s]) for s, m in metrics.items()}
        ranked = sorted(metrics, key=lambda s: -scores[s])
        eligible = [s for s in ranked if not reasons[s]]
        open_ = eligible[: u.open_tables]
        watch = [s for s in ranked if s not in open_][: u.watchlist]

        clusters = await self._clusters([*open_, *watch, "BTCUSDT"])
        prev = set(self.last.open) if self.last else set()
        res = ScanResult(
            now,
            metrics,
            scores,
            reasons,
            open_,
            watch,
            clusters,
            added=sorted(set(open_) - prev),
            removed=sorted(prev - set(open_)),
        )
        self.last = res
        log.info("scan_done", candidates=len(cands), eligible=len(eligible), open=open_)
        return res

    async def _symbol_metrics(
        self, si: SymbolInfo, quote_volume: float, funding: float, now: int
    ) -> TableMetrics:
        sym = si.symbol
        depth, k1, k5, oi = await asyncio.gather(
            self._get("/fapi/v1/depth", {"symbol": sym, "limit": 50}),
            self._get("/fapi/v1/klines", {"symbol": sym, "interval": "1m", "limit": 99}),
            self._get("/fapi/v1/klines", {"symbol": sym, "interval": "5m", "limit": 30}),
            self._get("/fapi/v1/openInterest", {"symbol": sym}),
        )
        bids = [(float(p), float(q)) for p, q in depth["bids"]]
        asks = [(float(p), float(q)) for p, q in depth["asks"]]
        mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else float(k1[-1][4])
        dbid, dask = depth_within_bps(bids, asks, 10)
        snap_spread = (asks[0][0] - bids[0][0]) / mid * 1e4 if bids and asks else 99.0
        st = self.state.symbols.get(sym)
        has_hour = bool(st and st.spreads) and (
            st is not None and st.spreads[-1][0] - st.spreads[0][0] >= 3_600_000
        )
        med = self.state.median_spread_bps(sym, now - DAY_MS) if has_hour else None
        spread = med if med is not None else snap_spread

        def ohlc(k: list[list[Any]]) -> tuple[list[float], list[float], list[float]]:
            return [float(x[2]) for x in k], [float(x[3]) for x in k], [float(x[4]) for x in k]

        h1, l1, c1 = ohlc(k1)
        h5, l5, c5 = ohlc(k5)
        atr1 = atr(h1, l1, c1) / mid * 1e4
        atr5 = atr(h5, l5, c5) / mid * 1e4
        atr_bps: dict[str, float] = {}
        for tf, _ in game_tf_pairs(self.cfg):
            secs = tf_seconds(tf)
            atr_bps[tf] = atr5 if secs == 300 else atr1 * math.sqrt(secs / 60)

        oi_now = float(oi["openInterest"])
        prev = self._prev_oi.get(sym)
        self._prev_oi[sym] = oi_now
        fees = self.meta.fees.get(sym, FALLBACK_FEES)
        return TableMetrics(
            symbol=sym,
            price=mid,
            quote_volume=quote_volume,
            depth_bid_usd=dbid,
            depth_ask_usd=dask,
            spread_bps=spread,
            tick_bps=si.tick_bps(mid),
            maker_fee_bps=fees.maker_bps,
            taker_fee_bps=fees.taker_bps,
            slippage_bps=self.cfg.universe.slippage_model_bps,
            funding_rate=funding,
            atr_bps=atr_bps,
            oi_change=(oi_now / prev - 1) if prev else None,
            anomaly=wick_anomaly(h1, l1, recent=self.cfg.universe.anomaly_lookback_min),
        )

    async def _clusters(self, symbols: list[str]) -> list[set[str]]:
        syms = sorted(set(symbols))
        lim = self.cfg.universe.cluster_lookback_days * 24
        ks = await asyncio.gather(
            *(
                self._get("/fapi/v1/klines", {"symbol": s, "interval": "1h", "limit": lim})
                for s in syms
            )
        )
        rets = {}
        for s, k in zip(syms, ks, strict=True):
            c = np.array([float(x[4]) for x in k])
            if len(c) > 2:
                rets[s] = np.diff(np.log(c))
        return correlation_clusters(rets, self.cfg.universe.cluster_corr)
