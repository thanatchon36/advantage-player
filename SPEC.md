# SPEC — "Advantage Player" Futures Bot (Binance USDⓈ-M, multi-symbol)

## 0. ROLE, GOAL, PHILOSOPHY
You are a senior quant / trading-systems engineer. Build an async Python bot for Binance
USDⓈ-M perpetual futures that trades like a professional advantage gambler, not a
casino patron:

1. **Only bet when the edge is proven** (+EV after all costs). Otherwise sit out.
2. **Size every bet with fractional Kelly** on CONSERVATIVE edge estimates.
3. **Choose the table**: trade only the symbols / conditions where the edge exists.
4. **Record every decision**, including every decision NOT to bet.
5. **No tilt**: hard-coded rules that neither the bot nor I can break while losing.

Expect the bot to spend most of its time watching, not trading.
"No edge → no bet" is correct behavior, not a bug.

Before coding:
- If this folder already has code, read it and reuse what fits (esp. any Binance client).
- Use plan mode: propose architecture, modules, and phases; wait for my OK.
- Verify every endpoint, WS URL, limit, event name, and error code against the official
  docs (https://developers.binance.com/docs/derivatives/usds-margined-futures).
  Do NOT rely on memory or old blog examples.

---

## 1. EXCHANGE FACTS (verify each, then design around them)
- Conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET, STOP,
  TAKE_PROFIT) go through POST / DELETE /fapi/v1/algoOrder (algoType=CONDITIONAL).
  /fapi/v1/order rejects them (-4120). Untriggered algo orders can't be modified →
  cancel-and-replace.
- REST base: demo = https://demo-fapi.binance.com, live = https://fapi.binance.com.
  Look up the matching market-stream, user-data-stream and WS-API URLs for both.
  Check which symbols the demo environment actually lists.
- Max leverage per symbol comes from GET /fapi/v1/leverageBracket and falls as notional
  grows (altcoins are usually far below BTC). Never hardcode.
- Fees: GET /fapi/v1/commissionRate per symbol at startup and daily.
- Per-symbol filters from exchangeInfo: tickSize, stepSize, minNotional, status,
  onboardDate, deliveryDate. Round every price/qty.
- WS limits (streams per connection, inbound message rate, 24h forced disconnect):
  look them up and shard connections accordingly.
- Sub-minute bars: build locally from aggTrade.

---

## 2. STACK & CONFIG
- Python 3.12, asyncio (uvloop on Linux), httpx, websockets, pydantic-settings, structlog,
  SQLAlchemy, numpy / pandas / scipy, pyarrow, pytest + pytest-asyncio + hypothesis,
  ruff, mypy.
- Own thin HMAC-SHA256 signed client: server-time sync, recvWindow, weight / order-count
  tracking from response headers, backoff, idempotent newClientOrderId / clientAlgoId.
  If you use a library instead, prove it supports algoOrder first.
- Docker + docker-compose (bot, dashboard, db). Must also run natively on macOS
  (Apple Silicon).

`.env` (ship `.env.example`; never commit secrets):
```
BINANCE_ENV=demo                 # demo | live
BINANCE_DEMO_API_KEY=
BINANCE_DEMO_API_SECRET=
BINANCE_LIVE_API_KEY=
BINANCE_LIVE_API_SECRET=
LIVE_TRADING_CONFIRMED=false     # live refuses to start unless "true"
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=                # only this chat may issue commands
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8080
DASHBOARD_PASSWORD=
```
`config.yaml`: universe filters, games, risk / bankroll params, discipline params,
TARGET_LEVERAGE (a cap, default 50 — effective leverage is derived per trade, section 6).
Show a mode banner (DEMO / ⚠️ LIVE) in logs, Telegram, and the dashboard header.

---

## 3. TABLE SELECTION — universe scanner (runs every 15 min)
Universe: USDT-margined perpetuals with status TRADING, listed ≥ 30 days, no pending
delivery / delist.

Per symbol (rolling 24h unless noted):
- quote volume; order-book depth within ±10 bps of mid (each side)
- median spread_bps; tick_bps = tickSize / price × 1e4
- round_trip_cost_bps = 2 × fee_bps (maker or taker, per strategy) + spread_bps
  + modeled slippage_bps
- ATR_bps at each game timeframe
- **move_to_cost = ATR_bps(TF) / round_trip_cost_bps** ← the "table quality" score
- |funding rate|, open-interest change, recent wick / liquidation-cascade anomalies

Eligibility defaults: quote vol ≥ $50M, depth(±10 bps) ≥ $250k per side, tick_bps ≤ 2,
move_to_cost ≥ 5, |funding| ≤ 0.05% per interval, no anomaly in last 60 min.

Select top K = 8 "open tables" by score → subscribe full streams. Next 12 = watchlist
(shadow data only).

Correlation clusters: 1h returns over 7d; corr > 0.7 → same cluster (most alts cluster
with BTC). Clusters feed exposure caps (section 6).
Table changes are logged and sent to Telegram (debounced).

---

## 4. GAMES — every strategy is a hypothesis, not a belief
A **game** = (strategy, symbol, timeframe, regime).

Regime per symbol / TF:
- TREND: EMA20 / EMA50 aligned and |EMA20 slope| above threshold
- RANGE: low slope, price oscillating around VWAP
- HIGH_VOL: realized vol > 90th percentile of last 30d → no-trade by default

Strategy modules (pluggable: `on_bar(ctx) -> Signal | None`; Signal = side, entry type,
stop, targets, tag):
- **A — Trend pullback + candlestick trigger** (TREND; TF 15s / 1m / 5m)
  Pullback into VWAP / EMA20 zone (± 0.3 ATR). Trigger: engulfing, hammer / shooting
  star, or inside-bar breakout in trend direction. Confirm: volume z-score > 1.5 and
  aggressive-trade delta in trade direction. Stop beyond pattern extreme + 0.2 ATR.
  TP1 = 1.5R (close 50%, SL → BE), remainder TP2 = 2.5R or 1 × ATR trail. Time stop.
- **B — Order-flow imbalance, maker-first** (TREND / RANGE; TF 5s / 15s)
  Top-5 book imbalance > 0.7 persisting ≥ X s + same-side trade delta + spread = 1 tick.
  Post-only entry and post-only TP; stop = k × ATR. Track maker fill rate and adverse
  selection (price move against us right after fill).
- **C — Range fade** (RANGE; TF 1m / 5m)
  Price at VWAP ± 2σ band + rejection candle (pin bar / engulfing) + fading volume into
  the band. Target VWAP. Stop beyond band extreme + 0.2 ATR.
- **BASELINE — Random entry** (control group, always SHADOW)
  For each strategy: same symbols, regimes, frequency, sizing, and exit rules, but random
  side and timing.

All thresholds are parameters. Never tune them on the data used to evaluate them.

---

## 5. EDGE LEDGER — the core of the bot
Every closed trade (shadow, probe, or live) records its **R-multiple NET of fees,
slippage, and funding**, where 1R = planned loss at the stop.

Per game, rolling and lifetime:
- n, win%, avg win R, avg loss R, mean R (μ), std R (σ), E[R²] = σ² + μ²
- μ_low = one-sided 95% lower bound of μ (bootstrap; t-approximation fallback)
- p-value for H0: μ ≤ 0 (out-of-sample / shadow data only)
- Δ vs BASELINE = μ_game − μ_baseline, with its own lower bound
- profit factor, max consecutive losses, fee share of gross, slippage vs model,
  maker fill rate (B), MAE / MFE

**Multiple testing**: many games → some look good by pure luck. Apply Benjamini-Hochberg
(q = 0.10) across all games evaluated in the same window.
A game PASSES only if: BH-significant AND μ_low > 0 AND lower bound of Δ-vs-baseline > 0.

**Lifecycle** (status per game; persisted; every transition logged + sent to Telegram):
`RESEARCH → SHADOW → PROBE → LIVE → SUSPENDED → (back to SHADOW only)`
- RESEARCH: replay on recorded ticks with walk-forward (train / OOS). OOS must pass → SHADOW.
- SHADOW: runs on live market data with a conservative fill simulator (limit orders fill
  only when price trades THROUGH the level; market orders pay spread + modeled impact).
  n ≥ 200 and PASS → PROBE.
- PROBE: real orders at minimum size (risk 0.1% or minNotional, whichever larger).
  n ≥ 100, PASS, and real slippage ≤ 1.5× model → LIVE.
- LIVE: Kelly-sized (section 6).
- SUSPENDED when any: CUSUM edge-decay alarm on the R stream; rolling-100 μ < 0;
  slippage > 2× model; fee share > 50% of gross; table / regime no longer eligible.

Demo and live ledgers are separate. LIVE status in the live environment requires a passed
PROBE in the live environment — demo results alone never unlock live size.

---

## 6. BANKROLL & BET SIZING
Bankroll = futures wallet equity only. README must tell me to keep most capital outside
the futures wallet.

Risk per trade (fraction of bankroll lost if the stop hits):
```
f_kelly = μ_low / E[R²]                 # Kelly for R-multiple outcomes, conservative μ
f       = kelly_fraction × f_kelly × dd_multiplier
f       = clamp(f, 0, max_risk_per_trade)
if f == 0 or game.status != LIVE: NO_BET (log reason)
```
Defaults: kelly_fraction = 0.25, max_risk_per_trade = 1.0%, PROBE risk = 0.1%.
dd_multiplier by drawdown from equity peak: < 5% → 1.0; 5–10% → 0.5; ≥ 10% → HALT.

Position size: `qty = bankroll × f / |entry − stop|` → round to stepSize; below
minNotional → skip.

**Leverage is derived, not chosen:**
```
L_safe = 1 / (stop_dist / 0.6 + mmr + fee_buffer)   # stop ≤ 60% of liquidation distance
L      = min(TARGET_LEVERAGE, bracket_max(symbol, notional), L_safe)
margin = notional / L   → must be ≤ 5% of bankroll, else shrink size or skip
```
ISOLATED margin, one-way position mode, max 1 position per symbol.

Portfolio caps:
- total open risk (sum of f over open positions) ≤ 3%
- per correlation cluster ≤ 1.5%; net same-direction exposure ≤ 2%
- max 4 concurrent positions
- daily loss limit: 3% of start-of-day equity → no new entries until 00:00 UTC

**Take chips off the table**: when equity > sweep_baseline × 1.20, Telegram suggests moving
the excess to spot. Optional auto-transfer futures → spot (needs the universal-transfer
API permission; default OFF). Reset baseline after each sweep.

---

## 7. DISCIPLINE RULES — hard-coded; each has a unit test that fails if violated
1. **No martingale**: sizing inputs are only bankroll, ledger stats, drawdown, and caps.
   A loss streak can only DECREASE size. Test: size after a loss ≤ size before, all else equal.
2. **No averaging down**, no adding to losers, no pyramiding (default).
3. **Every position has an exchange-side SL** (STOP_MARKET closePosition=true,
   workingType=MARK_PRICE via algoOrder) confirmed within 2 s of fill, else flatten + alert.
4. **SL moves only toward profit** (BE / trail). Never widened, never removed while open.
5. **No revenge**: after a stop-out, that symbol is locked for 15 min. 4 consecutive losses
   bot-wide → 30 min pause + edge-decay check on the games involved.
6. Only LIVE games trade normal size; only PROBE games trade probe size; nothing else trades.
7. **Hard kill at 10% drawdown**: cancel all orders + algo orders, flatten, halt. Resume only
   via manual /resume, and not before 12 h have passed.
8. **Pre-commitment for the human**: risk-INCREASING config changes (risk %, Kelly fraction,
   caps, TARGET_LEVERAGE, enabling games or tables) are queued and apply only after 24 h,
   and are rejected while drawdown > 5%. Risk-DECREASING changes apply immediately.
   Config is versioned and hashed; every change is logged + sent to Telegram.
9. Telegram and dashboard controls can only REDUCE risk (pause, close, kill, lower risk).
   No command exists to increase risk.
10. Entry blockers: stale data, WS down, spread > threshold, HIGH_VOL regime, within ±M min
    of funding time, clock drift > 500 ms, low rate-limit headroom.
11. Graceful shutdown (SIGTERM) cancels pending entries but KEEPS SLs on the exchange.
    Never leave a position unprotected.

---

## 8. EXECUTION, STATE, DATA
- Entry per strategy: maker-first (post-only GTX with timeout) or taker. Taker fallback
  only if the signal is still valid and expected slippage < cap.
- TP via algoOrder (or reduce-only post-only limit for maker exits). BE move and trailing
  via cancel-and-replace.
- User data stream is the source of truth (order, account, algo-order events), with
  listenKey keepalive. REST reconciliation at startup and every N s (positions, open
  orders, open algo orders). Orphan position without SL → place SL or flatten.
  Restart-safe: never double-enter.
- Market data: sharded combined streams (aggTrade, bookTicker, markPrice@1s, top-of-book
  depth) for open tables; reconnect with backoff; proactive rotation before 24 h;
  per-symbol stale detection.
- Recorder: raw ticks to Parquet per symbol / day (feeds RESEARCH replay).
- Storage (SQLite WAL default, Postgres option): table_snapshots, signals (incl. NO_BET +
  reason), games (status history), orders, algo_orders, fills, trades (R, fees, funding,
  slippage, MAE / MFE, hold time, game id), ledger_stats, equity, risk_events,
  config_versions, latency.

---

## 9. TELEGRAM (async; throttled; never blocks trading)
Alerts:
- start / stop + mode + config hash
- position open / close: symbol, game, side, size, entry, L, SL, TP, liq price, f %,
  result in R and $, fees
- game status transitions; open-table changes
- important vetoes: SL failure, orphan position, kill, DD throttle step,
  config change queued / applied / rejected
- connectivity problems; sweep suggestions

Daily report at 00:00 UTC (also shown in Asia/Bangkok time): equity, net PnL,
**"rake paid"** (fees + funding) vs gross PnL, trades per game, ledger changes,
NO_BET counts by reason.

Commands (only from TELEGRAM_CHAT_ID): /status /pnl /ledger /tables /pause /resume
/close <symbol|all> /kill /risk (view + lower-only setters).

---

## 10. DASHBOARD (FastAPI + WebSocket push; single page; TradingView lightweight-charts;
password auth; bind localhost by default)
- **Header**: mode, bot state, equity, today PnL, drawdown + throttle level, open risk vs
  caps, daily-limit usage, WS health.
- **Casino Floor**: table ranking (score, move_to_cost, spread, depth, vol, funding,
  cluster), open / watch status.
- **Edge Ledger**: per game n, win %, μ, μ_low, CI, Δ vs baseline, BH pass, Kelly f,
  status + history; sortable; click → that game's equity curve and R histogram.
- **Bankroll**: equity and drawdown curves, sweep baseline, exposure by cluster / direction.
- **Live**: per-symbol chart with entries / exits and SL / TP / liquidation lines;
  open-position cards.
- **Behavior**: PnL by game / symbol / side / hour (Bangkok) / regime; MAE-MFE scatter;
  hold-time histogram; fee + slippage share of gross; shadow vs real slippage.
- **Discipline log**: NO_BET reasons, vetoes, config versions, queued changes.
- Controls are risk-reducing only, with a confirm dialog. CSV export.

---

## 11. QUALITY
- Unit tests: Kelly / sizing math, derived leverage + liquidation guard, all caps, every
  rule in section 7, ledger stats (bootstrap bound, BH), lifecycle transitions, fill
  simulator, scanner filters, reconciliation.
- Property-based tests (hypothesis): sizing never exceeds any cap for any input.
- Integration test on DEMO: a probe trade end-to-end (entry → SL / TP via algoOrder →
  close → ledger updated → Telegram sent).
- Store all timestamps in UTC; display Asia/Bangkok in the UI.
- README: setup, demo key creation, API permissions (futures ON, withdrawals OFF,
  IP whitelist; universal transfer only if auto-sweep is enabled), how to run,
  go-live checklist.

---

## 12. PHASES (stop after each; show test results and a short demo)
1. Skeleton, config, signed client, exchangeInfo / brackets / fees / time sync
2. Multi-symbol market data, tick recorder, bar builder, table scanner
3. Strategy modules + BASELINE, replay engine, edge-ledger stats + BH
4. SHADOW mode with fill simulator + game lifecycle
5. Bankroll / sizing engine, discipline rules, execution, reconciliation (PROBE on DEMO)
6. Telegram
7. Dashboard
8. Docker, README, go-live checklist

**Go-live gate**: ≥ 1 game passing PROBE on DEMO, ≥ 2 weeks of continuous running with zero
unprotected-position incidents and zero discipline-rule violations. LIVE then starts with
every game back at PROBE.