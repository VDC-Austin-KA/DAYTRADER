"""moomoo OpenD market-data provider (quotes, history, option chains).

Speaks the same interface as the Tradier layer in ``market_data.py`` so the
rest of the app can't tell them apart. All calls go through one persistent
``OpenQuoteContext`` to the OpenD gateway configured by ``MOOMOO_OPEND_*``
env vars; the gateway runs on a machine the user controls, logged in via
its GUI. Every function degrades to ``None``/empty on failure so a dead
gateway can never take the app down — the router in ``market_data.py``
then falls back to Tradier when a token is configured.

Option chains: moomoo returns the contract LIST from ``get_option_chain``
and live marks (bid/ask/last, IV, greeks, OI, volume) from
``get_market_snapshot``. Snapshots accept at most 400 codes per call, so
strikes are pre-filtered to a band around spot and batched.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import settings

log = logging.getLogger("daytrader.data.moomoo")

_SNAPSHOT_BATCH = 400
# Only quote strikes within this fraction of spot — the tradeable belt.
_STRIKE_BAND = 0.12

_lock = threading.Lock()
_quote_ctx = None

# Same column contract as the Tradier layer.
_CHAIN_COLUMNS = [
    "contractSymbol", "strike", "lastPrice", "bid", "ask",
    "impliedVolatility", "openInterest", "volume",
]


def configured() -> bool:
    return bool(settings.moomoo_opend_host)


def _us_code(symbol: str) -> str:
    return symbol if symbol.upper().startswith("US.") else f"US.{symbol.upper()}"


def _context():
    """Lazily build the persistent quote context (call under ``_lock``)."""
    global _quote_ctx
    if _quote_ctx is not None:
        return _quote_ctx
    from moomoo import OpenQuoteContext, SysConfig  # type: ignore[import-not-found]

    SysConfig.set_all_thread_daemon(True)
    _quote_ctx = OpenQuoteContext(
        host=settings.moomoo_opend_host, port=settings.moomoo_opend_port
    )
    return _quote_ctx


def _reset_context() -> None:
    global _quote_ctx
    ctx, _quote_ctx = _quote_ctx, None
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass


def _call(fn_name: str, *args, **kwargs):
    """Run one quote-context method; returns the data or None on any error."""
    if not configured():
        return None
    with _lock:
        try:
            from moomoo import RET_OK  # type: ignore[import-not-found]

            ctx = _context()
            out = getattr(ctx, fn_name)(*args, **kwargs)
            ret, data = out[0], out[1]
            if ret != RET_OK:
                log.warning("moomoo %s failed: %s", fn_name, data)
                return None
            return out[1] if len(out) == 2 else out[1:]
        except ImportError:
            log.warning("moomoo-api package not installed")
            return None
        except Exception as exc:
            log.warning("moomoo %s error: %s (resetting context)", fn_name, exc)
            _reset_context()
            return None


def status() -> dict:
    if not configured():
        return {
            "configured": False, "ok": False, "provider": "moomoo",
            "message": "MOOMOO_OPEND_HOST not set — start OpenD and point the "
                       "app at it for real-time moomoo data.",
        }
    snap = _call("get_market_snapshot", [_us_code("SPY")])
    ok = snap is not None and len(snap) > 0
    return {
        "configured": True, "ok": ok, "provider": "moomoo",
        "gateway": f"{settings.moomoo_opend_host}:{settings.moomoo_opend_port}",
        "message": "Connected to moomoo OpenD." if ok else
                   "OpenD gateway unreachable or not logged in — data falls "
                   "back to Tradier if a token is set.",
    }


def get_quote(symbol: str) -> float | None:
    snap = _call("get_market_snapshot", [_us_code(symbol)])
    if snap is None or len(snap) == 0:
        return None
    row = snap.iloc[0]
    price = row.get("last_price") or row.get("prev_close_price")
    return float(price) if price else None


def get_history(symbol: str, years: int = 5, interval: str = "daily") -> pd.DataFrame:
    """Daily OHLCV via request_history_kline, paged, Title-cased columns."""
    try:
        from moomoo import KLType  # type: ignore[import-not-found]

        ktype = KLType.K_DAY
    except ImportError:
        return pd.DataFrame()
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=int(max(years, 1) * 365.25) + 5)

    frames: list[pd.DataFrame] = []
    page_key = None
    for _ in range(20):  # paging hard stop
        if not configured():
            return pd.DataFrame()
        with _lock:
            try:
                from moomoo import RET_OK  # type: ignore[import-not-found]

                ctx = _context()
                ret, df, page_key = ctx.request_history_kline(
                    _us_code(symbol), start=start.isoformat(), end=end.isoformat(),
                    ktype=ktype, max_count=1000, page_req_key=page_key,
                )
                if ret != RET_OK:
                    log.warning("moomoo history failed for %s: %s", symbol, df)
                    return pd.DataFrame()
            except Exception as exc:
                log.warning("moomoo history error: %s", exc)
                _reset_context()
                return pd.DataFrame()
        frames.append(df)
        if page_key is None:
            break
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    if out.empty or "time_key" not in out.columns:
        return pd.DataFrame()
    out = out.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    })
    out["Date"] = pd.to_datetime(out["time_key"])
    out = out.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]]
    return out.astype(float).sort_index()


def get_expirations(symbol: str) -> list[str]:
    df = _call("get_option_expiration_date", code=_us_code(symbol))
    if df is None or len(df) == 0 or "strike_time" not in df.columns:
        return []
    # OpenD includes already-expired cycles (negative distance); quoting them
    # yields a contract list with no marks, which reads as "no data".
    if "option_expiry_date_distance" in df.columns:
        df = df[pd.to_numeric(df["option_expiry_date_distance"], errors="coerce") >= 0]
    return sorted({str(d)[:10] for d in df["strike_time"].tolist()})


def get_option_chain(symbol: str, expiry: str) -> dict[str, pd.DataFrame]:
    """{'calls': df, 'puts': df} with live marks for near-the-money strikes."""
    empty = {
        "calls": pd.DataFrame(columns=_CHAIN_COLUMNS),
        "puts": pd.DataFrame(columns=_CHAIN_COLUMNS),
    }
    contracts = _call(
        "get_option_chain", code=_us_code(symbol), start=expiry, end=expiry
    )
    if contracts is None or len(contracts) == 0:
        return empty

    spot = get_quote(symbol)
    if spot:
        lo, hi = spot * (1 - _STRIKE_BAND), spot * (1 + _STRIKE_BAND)
        contracts = contracts[
            (contracts["strike_price"] >= lo) & (contracts["strike_price"] <= hi)
        ]
    codes = contracts["code"].tolist()
    if not codes:
        return empty

    snaps: list[pd.DataFrame] = []
    for i in range(0, len(codes), _SNAPSHOT_BATCH):
        snap = _call("get_market_snapshot", codes[i:i + _SNAPSHOT_BATCH])
        if snap is not None and len(snap):
            snaps.append(snap)
    if not snaps:
        return empty
    marks = pd.concat(snaps, ignore_index=True).set_index("code")

    def _num(row, col, default=0.0):
        try:
            v = row.get(col)
            return float(v) if v == v and v is not None else default
        except (TypeError, ValueError):
            return default

    calls_rows, puts_rows = [], []
    for _, c in contracts.iterrows():
        code = c["code"]
        if code not in marks.index:
            continue
        m = marks.loc[code]
        row = {
            "contractSymbol": code,
            "strike": float(c["strike_price"]),
            "lastPrice": _num(m, "last_price"),
            "bid": _num(m, "bid_price"),
            "ask": _num(m, "ask_price"),
            # moomoo reports option IV in percent; normalise to a fraction.
            "impliedVolatility": _num(m, "option_implied_volatility") / 100.0,
            "openInterest": int(_num(m, "option_open_interest")),
            "volume": int(_num(m, "volume")),
        }
        otype = str(c.get("option_type", "")).upper()
        if "CALL" in otype:
            calls_rows.append(row)
        elif "PUT" in otype:
            puts_rows.append(row)

    return {
        "calls": pd.DataFrame(calls_rows, columns=_CHAIN_COLUMNS),
        "puts": pd.DataFrame(puts_rows, columns=_CHAIN_COLUMNS),
    }
