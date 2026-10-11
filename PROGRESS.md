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
| 3 Strategies A/B/C + BASELINE, regime, fill simulator, engine, replay, ledger stats + BH | ✅ done | 81 passed (79 unit + 2 DEMO integration) |
| 4 SHADOW runner, game lifecycle, persistence, research loop, `run` command | ✅ done | 98 passed (96 unit + 2 DEMO integration) |
| 5 Sizing, derived leverage, caps, discipline §7, executor, UDS, reconciliation, PROBE on DEMO | ✅ done | 147 passed (144 unit + 3 DEMO integration) |
| 6 Telegram: throttled non-blocking alerts, debounced tables, daily report, chat-gated commands | ✅ done | 155 passed (152 unit + 3 DEMO integration) |

## Integration tests (skip if no DEMO keys)
- tests/integration/test_demo_metadata.py — time sync, exchangeInfo, brackets, fees (read-only)

## Integration tests also
- tests/integration/test_demo_streams.py — routed WS delivers all 4 event kinds (read-only)
- tests/integration/test_demo_probe_trade.py — REAL DEMO order flow: PROBE entry → SL
  (STOP_MARKET closePosition MARK_PRICE) + 2 TPs via algoOrder → make-before-break SL move →
  close → PROBE trade row + notifications; safety net flattens BTCUSDT on DEMO afterwards.
  If TELEGRAM_* is set it also delivers the OPEN/CLOSE alerts to the group (real sendMessage).

## Design decisions / approximations (not risk-changing)
- Table score = best move_to_cost over the (timeframe, entry) pairs that enabled games use;
  eligibility requires score ≥ 5. Each game re-checks its own TF in lifecycle (Phase 4).
- ATR at 5s/15s ≈ ATR_1m × √(tf/60) for scanning until local bars exist; 5m ATR from klines.
- Median spread from streamed bookTicker (≥ 1 h of samples) else depth snapshot.
- Anomaly = any 1m bar in last 60 min with range > 8 × median 1m range (99-bar window).
- Watchlist = next 12 by score regardless of eligibility; gets aggTrade + markPrice only.
- First DEMO scan (2026-10-11): 455 candidates ≥ $50M vol, 0 eligible (funding / tick / move_to_cost).
  "No edge → no bet" applies to tables too; SHADOW games can use watchlist data.

## Phase 3 notes
- Game id = `strategy|symbol|tf|regime`; BASELINE_X shadows each strategy per (symbol, tf),
  matching its observed signal rate (Beta(1,49) prior), stop distances (ATR units), targets,
  BE/trail and time stop; maker entries for B's baseline. Baselines are never promoted.
- Regime: WARMUP until EMA50 seeded AND ≥ 50 realized-vol samples (rv window 20 bars, sampled
  every 20 bars, 30-day history). Practical warm-up: 5s ≈ 1.4 h, 15s ≈ 4 h, 1m ≈ 17 h,
  5m ≈ 3.5 d → Phase 4 backfills 1m/5m from REST klines at startup.
- Fill sim: maker fills only on trade-through; market = half-spread + 1 bp impact; stop fills
  at stop level or the gap price (first price of the range); stop beats target in the same
  range; 1R = |planned entry − initial stop|; R net of fees, slippage (entry + exits), funding.
- fee_share = Σfees / Σgross R (1.0 if gross ≤ 0 — treated as "fees ate everything").
- μ_low: bootstrap (n ≥ 20) else Student-t; p: one-sided t-test; Δ-vs-baseline: bootstrap
  (both n ≥ 20) else Welch; BH q = 0.10 across non-baseline games in the same window.
- `python -m advantage_player research --days 14 BTCUSDT ETHUSDT SOLUSDT` on DEMO klines:
  288 trades, 12 games, 0 PASS (C on BTC ≈ −3 R OOS: tiny VWAP targets vs fees).

## Phase 4 notes
- Tables: signals (every decision incl. NO_BET + reason), games, game_transitions, trades
  (stage-tagged), ledger_stats, risk_events, equity, orders, algo_orders, fills, latency, kv.
  Every row carries `env` → DEMO and LIVE ledgers are separate.
- Trade stages: RESEARCH / SHADOW (sim while game in that status), BASELINE (control),
  SIM_PROBE / SIM_LIVE (sim twin of a real game, never counted), PROBE / LIVE (real fills, Phase 5).
- Promotion windows count only trades in the current stage since entering it; BH is applied
  across all games in that stage at each evaluation (every 15 min).
- Suspension applies to PROBE and LIVE games. Literal SPEC reading: a PROBE/LIVE game whose
  symbol leaves the open tables is SUSPENDED (risk-reducing). Rolling-100 μ check needs n ≥ 100;
  slippage check n ≥ 10; fee-share check n ≥ 20; CUSUM ref = stats at last promotion.
- SUSPENDED → SHADOW automatically after `suspend_cooldown_h` (24 h), fresh window (new config key).
- Go-live: `Lifecycle.seed_from(demo)` puts demo PROBE/LIVE games at PROBE in live, never LIVE.
- RESEARCH loop (daily): 30 d kline replay for open+watch tables → walk-forward OOS → BH →
  RESEARCH→SHADOW. Second-TF games (5s/15s) have no klines; they stay RESEARCH until tick
  research is wired (see open issues).
- Indicator warm-up: 2 d of 1m and 5 d of 5m klines per new table symbol.
- `python -m advantage_player run` smoke test on DEMO (3 min): routed WS healthy, ~25k events,
  research 5768 trades / 52 games / 0 pass.

## Phase 5 notes
- DEMO-verified order facts: -4120 (conditional on /order), -5022 (GTX would cross), -4046
  (margin type already set), -2022 (reduce-only w/o position), **-4130: only ONE closePosition
  STOP per direction** → SL moves are make-before-break: reduce-only qty SL at new level →
  cancel old → new closePosition SL → cancel temp (never unprotected). Intentional cancels are
  tracked so the "SL lost → restore" handler doesn't fire on them.
- UDS: `<ws>/private/ws/<listenKey>`; events ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ALGO_UPDATE
  (o.caid/aid/X/tp/cp), TRADE_LITE, ACCOUNT_CONFIG_UPDATE, listenKeyExpired. Keepalive 30 min.
- DEMO MARKET RESULT can return avgPrice 0 → fall back to GET order / positionRisk entryPrice.
- Finalize from authoritative /fapi/v1/userTrades since signal + FUNDING_FEE income; 1R =
  qty × |planned entry − initial stop|; R net of commissions and funding.
- Kelly input = (μ_low, E[R²]) of real PROBE+LIVE trades since the game last entered PROBE.
- Stop-out lock applies to stop / BE-stop / trail-stop exits (conservative reading).
- Caps shrink f to the remaining room (never exceed); PROBE size bumps to minNotional only if
  that risk stays ≤ max_risk_per_trade.
- Orphan position (not ours) without SL → flatten + alert; with an SL → left alone + alert.
  ⚠️ Don't trade manually on the bot's account.
- Transfers out (income TRANSFER, by tranId) lower peak / start-of-day so sweeps never look like
  drawdown; checked again right before any hard kill.
- Config pre-commitment: file is polled every 60 s; Telegram/dashboard lowers are stored as
  overrides on top of the file (removing one would be a queued increase).
- ⚠️ DECISION FOR USER (SPEC ambiguity, risk-relevant): after a 10 % hard kill, /resume (≥ 12 h)
  does NOT reset the equity peak, so entries stay blocked while drawdown ≥ 10 %. Chosen as the
  safest reading; alternative would be re-baselining the peak on resume.

## Phase 6 notes
- Bot @claude_code_crypto_dryrun_bot, group chat type `group`; privacy mode on (commands still
  delivered). Commands accepted only from TELEGRAM_CHAT_ID; others ignored + counted.
- notify() is enqueue-only (bounded 200, oldest dropped); sender ≥ 3 s apart, backlog
  coalesced, 429 retry_after honoured. Table changes: one message per 30 min (latest wins).
- Daily report at 00:00 UTC for the finished UTC day, shown with Bangkok time: equity, net,
  gross, rake (fees + funding paid), trades per game, ledger transitions, NO_BET by reason.
- Alerts: start/stop (+config hash), open/close (size, entry, L, SL, TP, liq, f%, R, $, fees),
  game transitions, table changes, SL failure / orphan / kill / DD step / config
  queued-applied-rejected, connectivity down/restored, sweep suggestions.

## Open issues
- Second-TF games (A@15s, B@5s/15s) can only leave RESEARCH via tick replay of recorded
  Parquet; `replay_ticks` exists but the daily research loop only uses klines so far.
- Table flapping (GRASSUSDT eligible → not, 70 s apart) causes WS resubscribe + table-change
  noise; Telegram debounce in Phase 6.

## End-of-run instruction (from user)
After final summary is written here and committed: stop any background processes I started
(dev servers, bots, watchers), then run `pmset sleepnow`.

## Next step
Phase 7 IN PROGRESS (stopped: usage limit). Done so far: deps fastapi/uvicorn/python-multipart
added (uncommitted code: none). Planned design:
- Dashboard reads ONLY the DB (works in-process or as separate compose service).
- Bot publishes kv "live_snapshot" (status, positions incl. liq, scan rows) every 5 s and
  kv "bars:<SYMBOL>" (last 200 1m bars) on each 1m close for open tables/positions.
- Controls: new table control_requests (id, ts, action, args, status, result); dashboard POST
  enqueues; bot polls every 2 s and runs them via risk.controls.Controls (reduce-only).
- Files: dashboard/backend.py (queries via reports.py + kv), dashboard/server.py (FastAPI:
  cookie login w/ HMAC token, /api/snapshot|ledger|game/{id}|bankroll|behavior|discipline|
  chart/{sym}|control, /api/export/{kind}.csv, WS /ws push every 2 s; refuse to start without
  DASHBOARD_PASSWORD; bind DASHBOARD_HOST), dashboard/static/index.html (lightweight-charts
  pinned version from unpkg, Bangkok time display, confirm dialogs).
- CLI: `dashboard` command and `run --dashboard`. Tests with fastapi TestClient.
Then Phase 8: Dockerfile + docker-compose (bot, dashboard, postgres), README (setup, demo key
creation, API permissions: futures ON, withdrawals OFF, IP whitelist, universal transfer only if
auto-sweep; keep most capital outside futures wallet; go-live checklist + gate), final summary,
then stop background processes and run `pmset sleepnow` (user instruction).
