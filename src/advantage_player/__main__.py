"""CLI entry point: `python -m advantage_player <command>`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from advantage_player.config.settings import Secrets, load_config
from advantage_player.exchange.endpoints import ENDPOINTS
from advantage_player.exchange.metadata import ExchangeMetadata
from advantage_player.exchange.rest import BinanceRest
from advantage_player.logging_setup import setup_logging
from advantage_player.marketdata.bars import Bar
from advantage_player.marketdata.hub import MarketDataHub
from advantage_player.marketdata.recorder import TickRecorder
from advantage_player.marketdata.state import MarketState
from advantage_player.scanner.persist import save_scan
from advantage_player.scanner.scanner import TableScanner, game_tf_pairs
from advantage_player.storage.db import make_engine, record_config_version

SAMPLE = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


async def cmd_info(config_path: str) -> int:
    secrets = Secrets()
    secrets.check_mode()
    cfg = load_config(config_path)
    ep = ENDPOINTS[secrets.binance_env]
    print(f"=== MODE: {secrets.banner} | config {cfg.hash} | {ep.rest} ===")
    engine = make_engine(cfg.storage.url)
    record_config_version(engine, cfg.hash, cfg.canonical(), "startup")

    rest = BinanceRest(
        ep.rest, secrets.api_key, secrets.api_secret, recv_window_ms=cfg.execution.recv_window_ms
    )
    try:
        await rest.sync_time()
        print(f"clock offset {rest.clock.offset_ms:+.0f} ms, rtt {rest.clock.rtt_ms:.0f} ms")
        meta = ExchangeMetadata(rest)
        if secrets.has_keys:
            await meta.refresh_all(SAMPLE)
        else:
            print("no API keys: skipping brackets / fees")
            await meta.refresh_exchange_info()
        perps = [s for s in meta.perpetuals() if s.status == "TRADING"]
        print(f"symbols listed: {len(meta.symbols)}, USDT perpetuals TRADING: {len(perps)}")
        for sym in SAMPLE:
            si = meta.symbols.get(sym)
            if si is None:
                print(f"{sym}: not listed")
                continue
            line = (
                f"{sym}: tick={si.tick_size} step={si.step_size} "
                f"minNotional={si.min_notional} status={si.status}"
            )
            if sym in meta.brackets:
                b = meta.brackets[sym]
                levs = {n: b.max_leverage(n) for n in (1_000, 100_000, 1_000_000)}
                line += f" maxLev@notional={levs}"
            if sym in meta.fees:
                f = meta.fees[sym]
                line += f" fees maker={f.maker_bps:.2f}bps taker={f.taker_bps:.2f}bps"
            print(line)
        print(f"rate-limit headroom: {rest.limiter.headroom():.1%}")
    finally:
        await rest.aclose()
    return 0


async def cmd_scan(config_path: str) -> int:
    secrets = Secrets()
    secrets.check_mode()
    cfg = load_config(config_path)
    ep = ENDPOINTS[secrets.binance_env]
    print(f"=== MODE: {secrets.banner} | config {cfg.hash} ===")
    rest = BinanceRest(ep.rest, secrets.api_key, secrets.api_secret)
    try:
        await rest.sync_time()
        meta = ExchangeMetadata(rest)
        await meta.refresh_exchange_info()
        if secrets.has_keys:
            await meta.refresh_brackets()
        scanner = TableScanner(rest, meta, MarketState(), cfg)
        res = await scanner.scan()
        save_scan(make_engine(cfg.storage.url), res)
        pairs = game_tf_pairs(cfg)
        print(f"candidates (vol ≥ min): {len(res.metrics)}  open: {res.open}")
        print(f"watch: {res.watch}")
        print(
            f"{'symbol':<14}{'score':>7}{'spread':>8}{'depth$k':>9}{'qv$M':>8}{'fund%':>8}"
            "  cl  reasons"
        )
        for sym in sorted(res.metrics, key=lambda x: -res.scores[x])[:30]:
            m = res.metrics[sym]
            print(
                f"{sym:<14}{res.scores[sym]:>7.2f}{m.spread_bps:>8.2f}"
                f"{min(m.depth_bid_usd, m.depth_ask_usd) / 1e3:>9.0f}{m.quote_volume / 1e6:>8.0f}"
                f"{m.funding_rate * 100:>8.4f}  {res.cluster_of(sym):>2}  "
                f"{','.join(res.reasons[sym])}"
            )
        best = {tf: e for tf, e in pairs}
        print(f"game (tf, entry) pairs: {sorted(best.items())}")
        print(f"clusters: {[sorted(c) for c in res.clusters if len(c) > 1]}")
        print(f"rate-limit headroom: {rest.limiter.headroom():.1%}")
    finally:
        await rest.aclose()
    return 0


async def cmd_stream(config_path: str, seconds: int, symbols: list[str]) -> int:
    secrets = Secrets()
    secrets.check_mode()
    cfg = load_config(config_path)
    ep = ENDPOINTS[secrets.binance_env]
    tfs = {tf for g in cfg.games.values() for tf in g.timeframes}
    rec = TickRecorder(cfg.storage.data_dir, flush_s=10)
    hub = MarketDataHub(ep.ws_stream, tfs, rec)
    bars: dict[str, int] = {}
    last: dict[str, str] = {}

    def on_bar(b: Bar) -> None:
        key = f"{b.symbol}/{b.tf_s}s"
        bars[key] = bars.get(key, 0) + 1
        last[key] = f"o={b.open} c={b.close} v={b.volume:.4f} delta={b.delta:+.4f}"

    hub.bar_subscribers.append(on_bar)
    await hub.set_symbols(symbols, [])
    await hub.start()
    await asyncio.sleep(seconds)
    healthy = hub.streams.healthy
    await hub.stop()
    print(f"events: {hub.counts}")
    print(f"ws healthy at end: {healthy}  parquet files: {rec.files_written}")
    for k in sorted(bars):
        print(f"bars {k}: {bars[k]}  last {last[k]}")
    for sym in symbols:
        st = hub.state.get(sym)
        if st.book:
            print(
                f"{sym} spread {st.book.spread_bps:.2f}bps imbalance "
                f"{st.depth.imbalance() if st.depth else float('nan'):.2f}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="advantage_player")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show mode, clock, exchange metadata")
    sub.add_parser("scan", help="run one universe scan and print the table ranking")
    sp = sub.add_parser("stream", help="stream + record market data for N seconds")
    sp.add_argument("--seconds", type=int, default=30)
    sp.add_argument("symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    args = p.parse_args(argv)
    setup_logging(args.log_level)
    if args.cmd == "info":
        return asyncio.run(cmd_info(args.config))
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(args.config))
    if args.cmd == "stream":
        return asyncio.run(cmd_stream(args.config, args.seconds, args.symbols))
    return 1


if __name__ == "__main__":
    sys.exit(main())
