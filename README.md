# Advantage Player

An async Binance USDⓈ-M perpetual-futures bot built to act like an advantage player rather than a casino patron:
it bets only when an edge is **proven** net of all costs, sizes bets with **fractional Kelly** on a conservative edge
estimate, chooses its tables, records every decision (including every decision *not* to bet), and follows
hard-coded discipline rules that neither it nor you can break while losing.

It should spend most of its time watching. "No edge → no bet" is correct behaviour, not a bug.

Full requirements: [`SPEC.md`](SPEC.md). Build log, verified exchange facts and design decisions: [`PROGRESS.md`](PROGRESS.md).

---

## 1. How it works

| Stage | What happens |
|---|---|
| **Table selection** | Every 15 min the scanner ranks USDT perpetuals by `move_to_cost = ATR / round-trip cost`, filters on volume, depth, tick size, funding and anomalies, opens the top 8 tables and keeps the next 12 on a watchlist. |
| **Games** | A game is (strategy, symbol, timeframe, regime). Strategies: A (trend pullback), B (order-flow imbalance, maker-first) and C (range fade), plus a random-entry **BASELINE** control for each one. |
| **Edge ledger** | Every trade records its R-multiple net of fees, slippage and funding. A game passes only if it is Benjamini-Hochberg significant (q = 0.10), its 95 % lower bound μ_low > 0, **and** it beats the baseline. |
| **Lifecycle** | `RESEARCH → SHADOW → PROBE → LIVE → SUSPENDED → SHADOW`. RESEARCH runs a kline replay with a walk-forward split. SHADOW uses live data with a conservative fill simulator (n ≥ 200). PROBE places real minimum-size orders (n ≥ 100, slippage ≤ 1.5× the model). LIVE sizes positions with Kelly. |
| **Sizing** | `f = 0.25 × μ_low / E[R²] × dd_multiplier`, capped at 1 % per trade. Leverage is *derived* from the stop distance, the exchange bracket and a liquidation guard. Portfolio caps: total open risk 3 %, per correlation cluster 1.5 %, net direction 2 %, at most 4 positions, daily loss limit 3 %. |
| **Discipline** | No martingale, no averaging down. Every position gets an exchange-side STOP_MARKET SL confirmed within 2 s. The SL only moves toward profit. Revenge locks apply after losses. Hard kill at 10 % drawdown (resume after ≥ 12 h, manual only). Risk increases in config wait 24 h. Telegram and dashboard controls can only *reduce* risk. |

---

## 2. Setup

### 2.1 Binance DEMO account and API key

1. Log in to Binance and open **Demo Trading** (`https://demo.binance.com`). The bot talks to
   `https://demo-fapi.binance.com`.
2. In the demo account, open **API Management** → **Create API** and choose system-generated (HMAC) keys.
3. Set the key's permissions as below. Use the same settings later for a live key.

| Permission | Setting | Why |
|---|---|---|
| Enable Reading | ON | account, positions, fills, reconciliation |
| **Enable Futures** | **ON** | orders and algo (SL/TP) orders |
| **Enable Withdrawals** | **OFF, always** | the bot never needs it; a leaked key then can't drain the account |
| Enable Spot & Margin Trading | OFF | not used |
| **Restrict access to trusted IPs** | **ON** (live: required) | whitelist only the machine(s) that run the bot |
| Permits Universal Transfer | OFF, unless you enable `risk.auto_sweep` | only the optional futures → spot "take chips off the table" sweep uses it |

4. Copy the key and secret into `.env` (below). Never commit `.env`. It is gitignored; keep it `chmod 600`.

### 2.2 Keep most of your capital OUTSIDE the futures wallet

The bot's bankroll is **the USDⓈ-M futures wallet equity only**. Every percentage limit (1 % per trade, 3 % daily,
10 % kill) is measured against that wallet. Keep only what you are prepared to put at risk in it and leave the rest
in spot, savings or off-exchange. When equity grows past 1.2× the sweep baseline, Telegram suggests moving the excess
out. Transfers out are detected and never count as drawdown.

### 2.3 Configure

```bash
cp .env.example .env        # then fill in keys, Telegram and dashboard password
chmod 600 .env
```

| `.env` key | Meaning |
|---|---|
| `BINANCE_ENV` | `demo` (default) or `live` |
| `BINANCE_DEMO_API_KEY` / `_SECRET` | demo key from 2.1 |
| `BINANCE_LIVE_API_KEY` / `_SECRET` | leave empty until the go-live gate (section 6) is passed |
| `LIVE_TRADING_CONFIRMED` | live refuses to start unless this is exactly `true` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | alerts and commands; commands are accepted **only** from this chat |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | default `127.0.0.1:8080` (localhost only) |
| `DASHBOARD_PASSWORD` | required; the dashboard refuses to start without it |
| `POSTGRES_PASSWORD` | docker-compose only |

`config.yaml` holds the universe filters, games, ledger thresholds, risk, discipline and execution settings. The
defaults mirror the SPEC. The file is polled every 60 s and every change is versioned and hashed:

- **Risk-decreasing** edits apply immediately.
- **Risk-increasing** edits (risk %, Kelly fraction, caps, `target_leverage`, enabling games or tables) are
  queued for **24 h**, and are **rejected** while drawdown > 5 %. Every queued, applied or rejected change is
  sent to Telegram.

`STORAGE_URL` / `DATA_DIR` environment variables override `storage.url` / `storage.data_dir` (Docker uses this for Postgres).

---

## 3. Running

### 3.1 Natively (macOS Apple Silicon or Linux)

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 itself.

```bash
uv sync
uv run python -m advantage_player info          # mode banner, clock offset, exchange metadata, fees
uv run python -m advantage_player scan          # one universe scan, prints the table ranking
uv run python -m advantage_player stream --seconds 60 BTCUSDT ETHUSDT   # stream + record ticks
uv run python -m advantage_player research --days 14 BTCUSDT ETHUSDT    # kline replay, walk-forward, BH
uv run python -m advantage_player run --dashboard                       # the bot + dashboard
uv run python -m advantage_player dashboard     # dashboard only (reads the DB; bot may run elsewhere)
```

`run` runs every game in SHADOW and executes real orders only for PROBE/LIVE games. Stop it with Ctrl-C or SIGTERM:
pending entries are cancelled and **exchange-side SLs stay in place**. Default storage is SQLite (WAL) at
`data/advantage_player.db`. Raw ticks go to `data/ticks/` as Parquet.

### 3.2 Docker (bot + dashboard + Postgres)

```bash
docker compose up -d --build
docker compose logs -f bot
# dashboard → http://127.0.0.1:8080 (published on localhost only)
docker compose down            # graceful: SIGTERM with a 60 s grace period, SLs stay on the exchange
```

The image runs on uvloop (Linux). `config.yaml` is mounted read-only from the repo, so editing it on the host still
goes through the pre-commitment rules. Secrets come from `.env` at runtime and are never baked into the image.

### 3.3 Dashboard

Log in with `DASHBOARD_PASSWORD`; the session cookie lasts 12 h. Times are shown in Asia/Bangkok and stored in UTC.

- **Header**: mode, bot state, equity, today's PnL, drawdown and throttle, open risk against caps, daily-limit
  usage, WebSocket health.
- **Tabs**: Casino Floor (table ranking) · Edge Ledger (sortable; click a game for its R curve and histogram) ·
  Bankroll (equity, drawdown, exposure by cluster and direction) · Live (1m chart with entry/SL/TP/liquidation
  lines, position cards) · Behavior (PnL by game/symbol/side/hour/regime, MAE–MFE, hold time, fee and slippage
  share) · Discipline log (NO_BET reasons, vetoes, config versions, queued changes, CSV export).
- **Controls** (each needs a confirm): pause, close a symbol or all, KILL, lower the risk scale, lower a setting.
  The dashboard has no control that increases risk.

To reach a remote dashboard, use an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 host`) instead of binding to `0.0.0.0`.

### 3.4 Telegram

Alerts: start/stop, position open/close (size, entry, leverage, SL, TP, liquidation price, f %, result in R and $,
fees), game transitions, table changes, SL failures, orphans, kill, drawdown steps, config changes, connectivity and
sweep suggestions. A daily report arrives at 00:00 UTC (07:00 Bangkok).

Commands (only from `TELEGRAM_CHAT_ID`): `/status /pnl /ledger /tables /pause /resume /close <SYMBOL|all> /kill /risk`.
`/risk` shows the settings and only *lowers* them (`/risk scale 0.5`, `/risk set risk.max_risk_per_trade 0.005`).

### 3.5 ⚠️ Don't trade manually on the bot's account

The bot reconciles positions and open orders every 30 s and on every user-stream reconnect. A position it did not
open and that has no SL is an **orphan**: the bot flattens it and alerts you. Use a separate (sub-)account for manual
trading.

---

## 4. Tests

```bash
uv run ruff check . && uv run mypy src tests && uv run pytest -q
```

- Unit tests cover Kelly and sizing math, derived leverage and the liquidation guard, every cap, every SPEC §7
  discipline rule, ledger stats (bootstrap bound, BH), lifecycle transitions, the fill simulator, scanner filters,
  reconciliation, Telegram and the dashboard (auth, risk-reducing-only controls, CSV, snapshot shape). Hypothesis
  property tests check that sizing never exceeds any cap.
- Integration tests (`tests/integration`) **place real orders on DEMO** (BTCUSDT) and send real Telegram messages.
  They are skipped when no DEMO keys are configured, or with `AP_SKIP_INTEGRATION=1`. **Don't run them while the bot
  is running on the same account**: the bot treats the test's position as an orphan.
- Postgres storage test: `AP_TEST_PG_URL=postgresql+psycopg://ap:ap@127.0.0.1:5432/ap uv run pytest tests/integration/test_postgres_storage.py`.

---

## 5. Operating notes

- **Hard kill (10 % drawdown)**: the bot cancels all orders and algo orders, flattens and halts. `/resume` works only
  after ≥ 12 h. The equity peak is **not** reset on resume, so entries stay blocked while drawdown is still ≥ 10 %
  (the conservative reading of the SPEC; see the open decision in PROGRESS.md).
- **Demo and live ledgers are separate.** Demo results alone never unlock live size. A fresh live environment
  currently starts **every game at RESEARCH** and has to re-earn SHADOW → PROBE → LIVE on live data.
  `Lifecycle.seed_from(demo)` (start demo-proven games at PROBE in live, as SPEC §12 describes) exists and is
  unit-tested, but is **not wired to any command yet**. That is a deliberate open decision, because it lets live
  real-money probes start sooner.
- Rotate the demo API key and Telegram token before going live if they were ever shared or pasted anywhere.

---

## 6. Go-live checklist

Live trading stays off until **every** box is ticked.

**Go-live gate (SPEC §12)**
- [ ] At least one game has **passed PROBE on DEMO** (BH-significant, μ_low > 0, beats baseline, n ≥ 100, real
      slippage ≤ 1.5× model).
- [ ] At least **2 weeks of continuous running** on DEMO.
- [ ] **Zero unprotected-position incidents** in that period (no `sl_failure`, no unexplained `flatten` /
      orphan events in Dashboard → Discipline log or `risk_events`).
- [ ] **Zero discipline-rule violations** in that period.
- [ ] LIVE starts with no game above PROBE. Today every game starts at RESEARCH in live (stricter); seeding demo
      PROBE/LIVE games to PROBE needs `Lifecycle.seed_from` to be wired in first (see section 5).

**Account and keys**
- [ ] Live API key: Futures ON, **Withdrawals OFF**, **IP whitelist ON**, Universal Transfer only if `auto_sweep: true`.
- [ ] Futures wallet holds only risk capital; the rest is outside it.
- [ ] One-way position mode, and no manual positions or orders on this account.
- [ ] Fresh Telegram token; `TELEGRAM_CHAT_ID` is a private chat or group only you control.
- [ ] Strong `DASHBOARD_PASSWORD`; the dashboard is bound to localhost (or reached through an SSH tunnel).

**Configuration**
- [ ] `config.yaml` risk and discipline values reviewed. Defaults: Kelly 0.25, max 1 % per trade, PROBE 0.1 %,
      caps 3 % / 1.5 % / 2 %, at most 4 positions, daily loss 3 %, kill at 10 %.
- [ ] `uv run python -m advantage_player info` with `BINANCE_ENV=live` shows the **⚠️ LIVE** banner, the correct
      fees and the leverage brackets.
- [ ] The test suite is green.

**Switch**
- [ ] Set `BINANCE_ENV=live`, `BINANCE_LIVE_API_KEY/SECRET` and `LIVE_TRADING_CONFIRMED=true` (live refuses to
      start without it).
- [ ] Watch the first PROBE trades end to end: entry, SL confirmed within 2 s, TP, close, ledger row, Telegram alert.
