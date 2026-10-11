# PROGRESS

Resume protocol: re-read SPEC.md and this file first, then continue at "Next step".
Mode: DEMO only. Phases 2–8 run back-to-back (user instruction overrides SPEC §12 "stop after each").

## Verified exchange facts (official docs + live DEMO probes, 2026-10-11)
- REST demo `https://demo-fapi.binance.com`, live `https://fapi.binance.com`
- Market WS demo `wss://demo-fstream.binance.com`, live `wss://fstream.binance.com`;
  routed paths `/public` (depth, book), `/market` (aggTrade, markPrice…), `/private` (user data)
- WS: 1024 streams/conn, 10 inbound msgs/s, 24 h lifetime, server ping 3 min / pong ≤ 10 min
- WS-API live `wss://ws-fapi.binance.com/ws-fapi/v1`; demo WS-API not documented (unused)
- Demo exchangeInfo: 742 symbols, 528 USDT perpetuals TRADING; rate limits 6000 weight/min,
  1200 orders/min, 300 orders/10 s
- Filters: PRICE_FILTER.tickSize, LOT_SIZE.stepSize, MARKET_LOT_SIZE, MIN_NOTIONAL.notional
- algoOrder POST/DELETE/GET /fapi/v1/algoOrder, DELETE /fapi/v1/algoOpenOrders, clientAlgoId
  regex `^[\.A-Z\:/a-z0-9_-]{1,36}$`
- listenKey POST/PUT/DELETE /fapi/v1/listenKey, 60 min expiry
- Demo fees (BTC/ETH/SOL): maker 2.00 bps, taker 4.00 bps

## Phase status
| Phase | Status | Tests |
|---|---|---|
| 1 Skeleton, config, signed client, metadata, time sync | ✅ done | 29 passed (28 unit + 1 DEMO integration) |

## Integration tests (skip if no DEMO keys)
- tests/integration/test_demo_metadata.py — time sync, exchangeInfo, brackets, fees (read-only)

## Open issues
- None blocking.

## Next step
Phase 2: multi-symbol market data (sharded routed WS), tick recorder (Parquet), bar builder
(aggTrade → 5s/15s/1m/5m), table scanner + clusters.
