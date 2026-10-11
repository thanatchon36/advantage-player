"""Base URLs per environment. Verified against Binance USDⓈ-M docs on 2026-10-11.

Market streams use routed paths: /public (high-frequency book data), /market (aggTrade,
markPrice, ...), /private (user data). A connection without a routed path receives only
public data.
"""

from __future__ import annotations

from dataclasses import dataclass

from advantage_player.config.settings import Env


@dataclass(frozen=True)
class Endpoints:
    rest: str
    ws_stream: str
    ws_api: str | None  # demo WS-API URL is not documented; we only use REST + streams


ENDPOINTS: dict[Env, Endpoints] = {
    Env.DEMO: Endpoints(
        rest="https://demo-fapi.binance.com",
        ws_stream="wss://demo-fstream.binance.com",
        ws_api=None,
    ),
    Env.LIVE: Endpoints(
        rest="https://fapi.binance.com",
        ws_stream="wss://fstream.binance.com",
        ws_api="wss://ws-fapi.binance.com/ws-fapi/v1",
    ),
}
