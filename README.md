# ⚡ ML Options Day Trader

A web-based, machine-learning-powered **paper-trading** platform focused on
**short-dated options (expiring in less than one month)**. It models short-term
directional moves with a gradient-boosting classifier, scans the live options
chain for liquid near-the-money contracts under 30 DTE, and lets you paper-trade
the resulting signals — all from a single dashboard. Designed to deploy on
[Railway](https://railway.app) in one click.

> ⚠️ **Educational / paper-trading only. Not financial advice.** No real
> brokerage orders are ever placed. Options trading carries substantial risk of
> loss. Past model performance does not predict future results.

![dashboard](https://img.shields.io/badge/stack-FastAPI%20%2B%20scikit--learn-blue)

## What it does

## ⚙️ Required: connect market data (Tradier)

The app needs a market-data source. It uses **[Tradier](https://developer.tradier.com)**,
which has a **free** developer token and provides real option chains (with greeks)
plus historical prices — and, unlike free keyless sources (Yahoo/Stooq), it works
reliably from cloud hosts like Railway.

1. Create a free account at **https://developer.tradier.com**.
2. Create an app and copy the **Access Token**.
3. Set it as an environment variable / Railway service variable:
   ```
   TRADIER_TOKEN=your_token_here
   TRADIER_BASE_URL=https://sandbox.tradier.com/v1   # sandbox = delayed quotes, real chains
   ```
4. Redeploy. On startup the app auto-trains models for the watchlist, then
   populates signals. The dashboard shows a banner if the token is missing or invalid.

> Sandbox gives delayed quotes and real option chains — perfect for paper trading.
> For real-time data, use a funded Tradier brokerage token with
> `TRADIER_BASE_URL=https://api.tradier.com/v1`.

## What it does

| Layer | Detail |
|-------|--------|
| **Data** | Historical prices + live option chains (with greeks) via the **Tradier** API |
| **Features** | RSI, MACD, Bollinger position, ATR%, moving-average ratios, momentum, realized volatility, volume ratio |
| **Model** | `GradientBoostingClassifier` per symbol, predicting whether the next *N*-day return clears a target move; time-ordered train/validation split |
| **Signals** | Model probability → bullish/bearish/neutral bias → recommended **call/put** contract |
| **Options filter** | Only contracts with **1 ≤ DTE ≤ 30**, near-the-money, ranked by open interest / volume / spread |
| **Opportunity scanner** | Per-ticker scan of **cheap ≤3-DTE options** (< $1.00 / < $100), ranked by **probability of profit × potential return**, with a Black-Scholes POP so the risk is visible |
| **Paper trading** | Cash-tracked portfolio, open/close positions, mark-to-market, realized & unrealized P&L |
| **Scheduler** | Background signal refresh + weekly retrain (APScheduler) |
| **Frontend** | Single-page dashboard with Chart.js price charts, signals table, positions, model metrics, trade log |

## 🔥 Market Movers — universe-wide options ranking

The dashboard's top panel sweeps a watchlist of high-volume, high-beta names
(`MOVERS_WATCHLIST` — semis/memory: NVDA, AMD, INTC, MU, SNDK, WDC, SMCI,
AVGO; megacaps META, TSLA; leveraged/sector ETFs SOXL, SOXS, GUSH; indexes
SPY, QQQ; DXYZ as the closest public SpaceX proxy; HXSCL for SK Hynix) and,
instead of showing one ticker's POP at a time, ranks **every contract across
the whole universe** by a blended likelihood-of-profit score. Each column is
click-sortable and has its own filter box.

**Surge Score (0-100)** — a custom pre-move indicator computed per name from
signals that have historically preceded outsized moves:

| Weight | Component | Signal |
|--------|-----------|--------|
| 40% | Squeeze | Bollinger-width percentile vs the past year — tight coils precede expansions |
| 30% | Burst | Today's move vs its own ATR + volume vs 20-day average — volume confirms ignition |
| 30% | Momentum | z-score of the 5-day rate of change — persistent pressure, signed for direction |

A **whipsaw gauge** (Kaufman efficiency ratio < 0.35 with elevated ATR) flags
names swinging hard both ways. For those, the suggested play is two-step:
buy the option on the signal leg, and once a swing runs favorably, **sell a
further-OTM contract of the same type/expiry for ≥ your entry cost** — the
vertical is then free and the chop can't hurt you. Play cards show the exact
wing strike; a **headline ticker bar** across the top streams names whose
surge just jumped, hot movers, and the current top plays.

`GET /api/movers` returns the whole payload (readings, ranked options, plays,
headlines); results are cached ~2 minutes to respect Tradier rate limits.
Surge scores are statistical tendencies, not guarantees.

## 🤖 BTC hourly prediction-market bot

An autonomous bot (in `app/prediction/`) that trades **BTC hourly
prediction-market contracts** — the moomoo prediction markets, which are
Kalshi event contracts (series `KXBTCD`, "Bitcoin above $X at the hour").
Every cycle (default 60 s) it:

1. Settles any due positions against the BTC index price at the hour boundary.
2. Runs the risk gates: max open positions, max daily loss, consecutive-loss
   cooldown, pause switch.
3. Fetches the BTC spot (Coinbase public API, keyless) and recent 1-minute
   volatility/momentum, then computes a closed-form probability that the
   nearest-strike contract settles YES.
4. Compares that probability with the market-implied one (the ask) and, when
   the edge clears `PREDICTION_MIN_EDGE`, buys YES or NO sized by capped
   fractional Kelly.

Execution modes (`PREDICTION_TRADE_MODE`):

- **`paper`** (default) — fills simulated locally at the quoted ask and
  settled against the real BTC index. No orders leave the app.
- **`live`** — orders route through the **moomoo OpenD gateway** using the
  `MOOMOO_*` environment variables (host/port, account, trade environment).
  If the gateway is unreachable or misconfigured the bot logs an error and
  falls back to paper rather than crashing (order attempts are hard-capped at
  30 s so a dead gateway can never wedge the loop). Note: moomoo's published
  OpenAPI does not yet document event-contract order support — keep
  `MOOMOO_CODE_PREFIX` and `MOOMOO_TRD_ENV=SIMULATE` until you have verified
  order routing against your account.

### Connecting moomoo OpenD (for live mode)

moomoo's official AI setup installs two Claude skills — `install-moomoo-opend`
(gateway installer) and `moomooapi` (market data & trading) — via the
[one-click guide](https://openapi.moomoo.com/moomoo-api-doc/en/intro/ai.html)
(skills package: `https://openapi.moomoo.com/skills/opend-skills.zip`).

OpenD itself has two hard, by-design manual gates that no automation can (or
should) bypass:

1. **Login** — the OpenD GUI requires your moomoo account credentials (and a
   captcha/device verification) after every fresh start.
2. **Trade unlock** — trading must be unlocked by hand in the OpenD GUI;
   moomoo's security policy forbids SDK-based `unlock_trade`, and this app
   deliberately contains no such call.

Because Railway containers are ephemeral and headless, **run OpenD on a
machine you control** (desktop, home server, or a VPS with a desktop/VNC):
log in, unlock trading, set the listen address so the gateway is reachable by
the app (secure the network path — VPN/tailnet, never the open internet),
then set `MOOMOO_OPEND_HOST`/`MOOMOO_OPEND_PORT` on the Railway service. The
bot picks it up on the next cycle; until then it runs in paper mode.

Ops endpoints: `GET /api/prediction/status`, `GET /api/prediction/trades`,
`POST /api/prediction/pause|resume|run`. All strategy/risk knobs are env vars
(see `.env.example`). State lives in Postgres, so restarts/redeploys are safe
and a tripped daily-loss stop cannot be wiped by a restart.

## ⚡ Intraday 0-3 DTE vertical-spread bot (`app/spreads`)

A standalone asynchronous bot that trades defined-risk options verticals
(debit/credit spreads) intraday off moomoo OpenD's real-time options feed:

```bash
python -m app.spreads        # paper mode by default; needs MOOMOO_OPEND_HOST reachable
```

- **Low-latency ingestion** — moomoo's OpenAPI SDK is synchronous/callback-based,
  so push callbacks (`StockQuoteHandlerBase` for greeks/IV, `OrderBookHandlerBase`
  for top-of-book bid/ask) marshal onto the event loop via
  `loop.call_soon_threadsafe` into a bounded `asyncio.Queue` (drop-oldest
  backpressure — the SDK's push thread never blocks). A consumer coroutine
  drains the queue into an in-memory numpy options chain (bid/ask/mid, delta,
  gamma, IV per strike/right/expiry). A separate discovery task re-scans the
  chain every 15s for contracts entering/leaving the 0-3 DTE window (OpenD has
  no "new contract" push) and subscribes anything new in throttled batches.
- **IV-rank regime switching** — ATM IV is sampled into a rolling window
  (persisted across restarts). IV rank > 70 scans for **credit** spreads;
  IV rank < 20 scans for **debit** spreads; in between it stands down.
  Cold starts pre-seed the window from CBOE short-dated vol indices
  (`^VIX1D`, free via Yahoo) so day one trades instead of warming up;
  live chain samples then take over (`python -m app.spreads.seed_iv`
  re-seeds manually).
- **Delta-based strike selection** — short leg at the ~0.20 |delta| strike,
  wing 1-5 strikes away (configurable), liquidity and credit/width filters.
- **Guarded execution via moomoo OpenD** — orders go out only if the freshest
  tick behind each leg is under 150ms old (order-timing guardrail); limits are
  mid ± a tight slippage tolerance; the long leg always fills first so the
  book never carries a naked short; live mode reuses the same `MOOMOO_*`
  gateway config used for data (trading unlocked manually in the OpenD GUI,
  never via SDK).
- **Intraday watchdog** — every 2s: per-spread hard stop at 50% of defined max
  risk, a daily equity circuit breaker (-3% from the session open flattens
  everything and halts), and a maintenance-margin utilisation guard fed by the
  broker's portfolio-margin endpoint.

All knobs are `SPREADS_*` env vars — see `.env.example`. Paper mode fills at
the computed limits so the whole pipeline can be exercised without placing
real orders, but market data still requires a running, logged-in OpenD
gateway with an options-quote-entitled moomoo account (data streams
regardless of trade mode; only order placement needs `SPREADS_TRADE_MODE=live`
+ trading unlocked in the OpenD GUI).

> Polygon.io/Massive's REST and WebSocket options endpoints were evaluated
> first but this account's plan returns `403 NOT_AUTHORIZED` on options data
> (confirmed on both the direct API and the MCP server below); moomoo OpenD's
> own feed is fully entitled on this account and was used instead. See
> "Market-data research" below for where Polygon/Massive access remains
> useful even though it doesn't power live trading.

### Running the bot on Railway

The bot is a separate process from the web app (the `web` Procfile entry only
starts uvicorn). To run it deployed:

1. In the Railway project: **New → Service → GitHub repo** and pick this repo
   again (a second service on the same repo).
2. On that service, set **Start Command** to `python -m app.spreads` (overrides
   the `railway.json` web command; the `worker:` line in the Procfile documents
   the same thing).
3. Set `MOOMOO_OPEND_HOST`/`MOOMOO_OPEND_PORT` (and any `SPREADS_*` overrides)
   as service variables — OpenD must be reachable from Railway (VPN/tailnet to
   the machine running it; never expose it to the open internet). No
   healthcheck/port is needed on this service — it's a headless worker.
4. Leave `SPREADS_TRADE_MODE=paper` until the paper pipeline has run through
   full sessions cleanly; then flip to `live` (data streams the same way in
   both modes — only order placement changes).

### Market-data research: Massive MCP + S3 flat files

Two separate Polygon.io/Massive access paths are wired up for research and
backtesting during development — neither is used by the live trading path,
which sources data from moomoo OpenD instead (see above) because this
account's Massive plan lacks the REST/WebSocket options entitlement:

- **`.mcp.json`** registers [Massive](https://massive.com/docs/ai-tools/quickstart)'s
  remote MCP server for Claude Code sessions on this repo — lets Claude query
  market data conversationally. First use requires a one-time OAuth: run
  `claude`, type `/mcp`, select **massive**, authenticate.
- **S3 flat files** (`MASSIVE_S3_*` in `.env`, gitignored) grant bulk
  historical CSV dumps under `us_options_opra/` — day/minute aggregates,
  trades, and quotes back to 2014 — a different entitlement from the blocked
  REST/WS endpoints, confirmed working. This is the practical way to backtest
  the delta/wing/IV-rank parameters or pre-seed `iv_history.json`: point a
  standard S3 client (e.g. `boto3`) at `MASSIVE_S3_ENDPOINT`/`MASSIVE_S3_BUCKET`
  with the access/secret key pair. It's a static daily export, not a live
  feed, so it can't replace the moomoo ingestion path.

## Architecture

```
app/
├── main.py            FastAPI app + lifespan (init DB, start scheduler)
├── config.py          Env-driven settings (strategy params, watchlist)
├── database.py        SQLAlchemy engine/session (Postgres on Railway, SQLite locally)
├── models.py          ORM: Portfolio, Position, Trade, Signal, ModelMeta
├── schemas.py         Pydantic request/response models
├── training.py        Train orchestration + metric persistence
├── scheduler.py       APScheduler jobs
├── data/market_data.py   Tradier client with TTL caching
├── ml/features.py     Technical-indicator feature engineering
├── ml/model.py        Train / persist / predict
├── prediction/        BTC hourly prediction-market bot
│   ├── bot.py           decision cycle: settle → gate → estimate → trade
│   ├── model.py         closed-form hourly direction probability
│   ├── markets.py       contract discovery/quotes (Kalshi public API)
│   ├── execution.py     paper + moomoo OpenD executors
│   ├── risk.py          stops, daily loss limit, Kelly sizing
│   └── data.py          BTC spot/1-minute candles (Coinbase public API)
├── spreads/           Intraday 0-3 DTE vertical-spread bot (python -m app.spreads)
│   ├── bot.py           task orchestration (stream/consumer/discovery/scanner/watchdog)
│   ├── ingest.py        moomoo OpenD push (QUOTE + ORDER_BOOK) via asyncio.Queue backpressure
│   ├── chain.py         in-memory numpy options chain (bid/ask/delta/gamma/IV/tick ts)
│   ├── ivrank.py        rolling IV-rank tracker (persisted)
│   ├── scanner.py       regime switch + delta strike selection
│   ├── execution.py     mid±slippage limits, 150ms staleness guardrail, moomoo bridge
│   └── watchdog.py      position stops, equity/margin circuit breakers
├── trading/options.py Short-dated option-chain scanner
├── trading/signals.py Model + scanner -> signal
├── trading/paper.py   Paper-trading engine
├── routers/api.py     JSON API
├── routers/views.py   HTML dashboard
├── templates/         Jinja2
└── static/            CSS + dashboard JS
tests/                 Offline unit tests (no network)
```

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; SQLite is used by default
uvicorn app.main:app --reload
# open http://localhost:8000
```

First time: click **Train models** (downloads history and fits a model per
watchlist symbol), then **Refresh signals**.

## Deploy to Railway

1. Push this repo to GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** (it auto-detects the `Dockerfile`).
3. Add the **PostgreSQL** plugin — Railway injects `DATABASE_URL` automatically.
4. **Set `TRADIER_TOKEN`** (required — see "connect market data" above). Optionally
   set `WATCHLIST`, `STARTING_CASH`, etc. from `.env.example`.
5. Railway sets `$PORT`; the app binds to it. Health check: `/api/health`.

That's it — the app starts, creates its tables, trains models on first boot
(if none exist), and begins refreshing signals on the schedule.

## API quick reference

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Liveness probe |
| GET | `/api/config` | Active strategy parameters |
| POST | `/api/train` | Train models (`{"symbols": [...]}` optional) |
| GET | `/api/models` | Per-symbol accuracy / ROC-AUC |
| POST | `/api/signals/refresh` | Regenerate all signals |
| GET | `/api/signals` | Cached signals |
| GET | `/api/signal/{symbol}` | Live signal for one symbol |
| GET | `/api/opportunities/{symbol}` | **Rank cheap short-dated options** (params: `max_dte`, `max_premium`, `max_cost`, `side`, `limit`) |
| GET | `/api/options/{symbol}?direction=call` | Scan short-dated chain (single best pick) |
| GET | `/api/portfolio` | Summary + open positions |
| POST | `/api/trade` | Open a paper position |
| POST | `/api/close` | Close a position |
| GET | `/api/trades` | Trade log |

## Configuration (env vars)

See [`.env.example`](.env.example). Key knobs:

- `MAX_DTE` / `MIN_DTE` — the **< 1 month** window (default 1–30 days)
- `SIGNAL_THRESHOLD` — probability needed to emit a directional signal
- `HORIZON_DAYS` / `TARGET_MOVE` — what the model is trained to predict
- `WATCHLIST` — symbols to model and scan
- `STARTING_CASH` — paper-trading balance

## Tests

```bash
pytest -q
```

The suite is fully offline — it synthesizes price data, so no network or API
keys are required.

## Disclaimer

This software is provided for **educational and research purposes only**. It
does not constitute financial, investment, or trading advice. Nothing here is a
recommendation to buy or sell any security or option. You are solely responsible
for any decisions you make. The authors accept no liability for any losses.
