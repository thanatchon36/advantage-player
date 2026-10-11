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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="advantage_player")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="show mode, clock, exchange metadata")
    args = p.parse_args(argv)
    setup_logging(args.log_level)
    if args.cmd == "info":
        return asyncio.run(cmd_info(args.config))
    return 1


if __name__ == "__main__":
    sys.exit(main())
