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

| Layer | Detail |
|-------|--------|
| **Data** | Free daily history + live option chains via `yfinance` (no API key) |
| **Features** | RSI, MACD, Bollinger position, ATR%, moving-average ratios, momentum, realized volatility, volume ratio |
| **Model** | `GradientBoostingClassifier` per symbol, predicting whether the next *N*-day return clears a target move; time-ordered train/validation split |
| **Signals** | Model probability → bullish/bearish/neutral bias → recommended **call/put** contract |
| **Options filter** | Only contracts with **1 ≤ DTE ≤ 30**, near-the-money, ranked by open interest / volume / spread |
| **Paper trading** | Cash-tracked portfolio, open/close positions, mark-to-market, realized & unrealized P&L |
| **Scheduler** | Background signal refresh + weekly retrain (APScheduler) |
| **Frontend** | Single-page dashboard with Chart.js price charts, signals table, positions, model metrics, trade log |

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
├── data/market_data.py   yfinance wrapper with TTL caching
├── ml/features.py     Technical-indicator feature engineering
├── ml/model.py        Train / persist / predict
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
4. (Optional) Set service variables from `.env.example` (e.g. `WATCHLIST`, `STARTING_CASH`).
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
| GET | `/api/options/{symbol}?direction=call` | Scan short-dated chain |
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
