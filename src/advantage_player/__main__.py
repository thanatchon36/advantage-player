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


async def cmd_research(config_path: str, symbols: list[str], days: int) -> int:
    """RESEARCH: replay REST kline history (bar mode), walk-forward OOS, BH across games."""
    import time as _time

    from advantage_player.sim.engine import GameEngine
    from advantage_player.sim.fills import Costs, TradeResult
    from advantage_player.sim.replay import evaluate, fetch_klines, replay_bars

    secrets = Secrets()
    secrets.check_mode()
    cfg = load_config(config_path)
    ep = ENDPOINTS[secrets.binance_env]
    rest = BinanceRest(ep.rest, secrets.api_key, secrets.api_secret)
    end = int(_time.time() * 1000)
    start = end - days * 86_400_000
    results: list[TradeResult] = []
    try:
        meta = ExchangeMetadata(rest)
        if secrets.has_keys:
            await meta.refresh_fees(symbols)
        for sym in symbols:
            fees = meta.fees.get(sym)
            costs = Costs(
                maker_fee=fees.maker if fees else 0.0002,
                taker_fee=fees.taker if fees else 0.0005,
                slippage_bps=cfg.universe.slippage_model_bps,
            )

            def costs_of(_s: str, c: Costs = costs) -> Costs:
                return c

            eng = GameEngine(cfg, costs_of, bar_mode=True)
            eng.result_hooks.append(results.append)
            bars = []
            for tf in sorted(t for t in eng.timeframes if t.endswith("m")):
                bars += await fetch_klines(rest, sym, tf, start, end)
            replay_bars(eng, bars)
            print(f"{sym}: {len(bars)} bars replayed")
    finally:
        await rest.aclose()
    split = start + int((end - start) * cfg.ledger.walk_forward_train_frac)
    evs = evaluate(results, split, cfg.ledger.bh_q, cfg.ledger.confidence)
    print(f"trades: {len(results)}  games: {len(evs)}  (OOS from {split})")
    print(f"{'game':<34}{'n_oos':>6}{'μ':>8}{'μ_low':>8}{'p':>8}{'Δlow':>8}  BH  PASS")
    for e in sorted(evs, key=lambda e: e.oos.p_value):
        o = e.oos
        dl = f"{o.delta_low:8.3f}" if o.delta_low is not None else "     n/a"
        print(
            f"{e.game_id:<34}{o.n:>6}{o.mean:>8.3f}{o.mu_low:>8.3f}{o.p_value:>8.3f}{dl}"
            f"  {'Y' if e.bh_significant else '-'}   {'Y' if e.passed else '-'}"
        )
    return 0


def _dashboard_password(secrets: Secrets) -> str:
    pw = secrets.dashboard_password.get_secret_value()
    if not pw:
        raise SystemExit("DASHBOARD_PASSWORD is empty — refusing to start the dashboard")
    return pw


async def cmd_dashboard(config_path: str) -> int:
    """Standalone dashboard: reads only the DB, so it can run beside (or apart from) the bot."""
    from advantage_player.dashboard.server import serve

    secrets = Secrets()
    secrets.check_mode()
    pw = _dashboard_password(secrets)
    cfg = load_config(config_path)
    print(
        f"=== DASHBOARD {secrets.banner} | http://{secrets.dashboard_host}:"
        f"{secrets.dashboard_port} ==="
    )
    await serve(
        make_engine(cfg.storage.url),
        secrets.binance_env.value,
        pw,
        secrets.dashboard_host,
        secrets.dashboard_port,
    )
    return 0


async def cmd_run(config_path: str, seconds: int | None, dashboard: bool = False) -> int:
    import signal

    from advantage_player.app import Bot

    secrets = Secrets()
    pw = _dashboard_password(secrets) if dashboard else ""
    bot = Bot(secrets, load_config(config_path))
    print(f"=== MODE: {bot.banner} | config {bot.cfg.hash} ===")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bot.stopping.set)
    await bot.start()
    dash: asyncio.Task[None] | None = None
    if dashboard:
        from advantage_player.dashboard.server import serve

        dash = asyncio.create_task(
            serve(
                bot.db, bot.env, pw, secrets.dashboard_host, secrets.dashboard_port, bot.stopping
            )
        )
        print(f"dashboard on http://{secrets.dashboard_host}:{secrets.dashboard_port}")
    deadline = loop.time() + seconds if seconds else None
    while not bot.stopping.is_set():
        try:
            await asyncio.wait_for(bot.stopping.wait(), timeout=60)
        except TimeoutError:
            print(bot.status(), flush=True)
        if deadline and loop.time() >= deadline:
            break
    print(bot.status(), flush=True)
    await bot.stop()
    if dash is not None:
        await dash
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
    rp = sub.add_parser("research", help="kline replay + walk-forward + BH (RESEARCH)")
    rp.add_argument("--days", type=int, default=14)
    rp.add_argument("symbols", nargs="*", default=["BTCUSDT", "ETHUSDT"])
    runp = sub.add_parser("run", help="run the bot (SHADOW for all games; executes PROBE/LIVE)")
    runp.add_argument("--seconds", type=int, default=None, help="stop after N seconds")
    runp.add_argument("--dashboard", action="store_true", help="also serve the dashboard")
    sub.add_parser("dashboard", help="serve the dashboard (reads the DB only)")
    args = p.parse_args(argv)
    setup_logging(args.log_level)
    if args.cmd == "info":
        return asyncio.run(cmd_info(args.config))
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(args.config))
    if args.cmd == "stream":
        return asyncio.run(cmd_stream(args.config, args.seconds, args.symbols))
    if args.cmd == "run":
        return asyncio.run(cmd_run(args.config, args.seconds, args.dashboard))
    if args.cmd == "dashboard":
        return asyncio.run(cmd_dashboard(args.config))
    if args.cmd == "research":
        return asyncio.run(cmd_research(args.config, args.symbols, args.days))
    return 1


if __name__ == "__main__":
    sys.exit(main())
