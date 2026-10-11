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
- DEMO delivers every stream on every path (no routing enforcement); LIVE enforces it, so we
  route per docs: aggTrade/markPrice → /market, bookTicker/depth → /public
- Stream payloads (DEMO): aggTrade {a,p,q,T,m}, bookTicker {u,b,B,a,A,E}, depth5 {b,a,E},
  markPriceUpdate {p,i,r,T}
- REST weights measured: ticker/24hr(all)=40, premiumIndex(all)=10, depth@50=2,
  klines<100=1, klines 168=2
- DEMO quirks: /futures/data/openInterestHist returns "ok" (unsupported) → OI change computed
  scan-to-scan from /fapi/v1/openInterest; /fapi/v1/commissionRate returns HTTP 500 for some
  symbols (AWE, ETHFI, SOON, …) → conservative fallback fees (maker 2 bps / taker 5 bps)

## Phase status
| Phase | Status | Tests |
|---|---|---|
| 1 Skeleton, config, signed client, metadata, time sync | ✅ done | 29 passed (28 unit + 1 DEMO integration) |
| 2 Market data (routed sharded WS), tick recorder, bar builder, table scanner | ✅ done | 50 passed (48 unit + 2 DEMO integration) |

## Integration tests (skip if no DEMO keys)
- tests/integration/test_demo_metadata.py — time sync, exchangeInfo, brackets, fees (read-only)

## Integration tests also
- tests/integration/test_demo_streams.py — routed WS delivers all 4 event kinds (read-only)

## Design decisions / approximations (not risk-changing)
- Table score = best move_to_cost over the (timeframe, entry) pairs that enabled games use;
  eligibility requires score ≥ 5. Each game re-checks its own TF in lifecycle (Phase 4).
- ATR at 5s/15s ≈ ATR_1m × √(tf/60) for scanning until local bars exist; 5m ATR from klines.
- Median spread from streamed bookTicker (≥ 1 h of samples) else depth snapshot.
- Anomaly = any 1m bar in last 60 min with range > 8 × median 1m range (99-bar window).
- Watchlist = next 12 by score regardless of eligibility; gets aggTrade + markPrice only.
- First DEMO scan (2026-10-11): 455 candidates ≥ $50M vol, 0 eligible (funding / tick / move_to_cost).
  "No edge → no bet" applies to tables too; SHADOW games can use watchlist data.

## Open issues
- None blocking.

## End-of-run instruction (from user)
After final summary is written here and committed: stop any background processes I started
(dev servers, bots, watchers), then run `pmset sleepnow`.

## Next step
Phase 3: strategy modules A/B/C + BASELINE, regime classifier, replay engine (Parquet ticks),
edge-ledger stats (bootstrap μ_low, t fallback, p-value, Δ vs baseline, BH q=0.10).
