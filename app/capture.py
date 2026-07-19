"""Record microstructure at decision time.  Run:  python -m app.capture

WHY
---
Fitting the user's entry rule to price/volume failed: not one feature
reached |t|>=2 across 281 real decisions. The signal they were trading on
was never in the data -- it was order-book depth and tape aggression,
which nothing records for free after the fact. Historical L2 is the most
expensive data class in the industry; TradingView and friends are charting
platforms and expose no history or export for it.

The way out is not to buy the past but to record the present. OpenD
already streams, on the entitlement this account holds:

    ORDER_BOOK -> bid/ask with SIZE at each level
    TICKER     -> every print with ticker_direction (BUY = lifted the
                  offer, SELL = hit the bid) -- literal tape aggression

This daemon snapshots that continuously for the ATM SPY 0DTE contracts and
writes it to parquet. After a few weeks of normal trading the fills can be
joined to it, and the entry question becomes answerable with data that
actually contains the signal.

It places NO orders and needs no trading unlock. Read-only.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import settings

log = logging.getLogger("daytrader.capture")

OUT_DIR = Path("capture")
SNAPSHOT_SECS = 2.0          # book snapshot cadence
FLUSH_EVERY = 150            # rows between parquet flushes
TAPE_WINDOW = 60.0           # seconds of prints kept for aggression stats


def _tape_stats(prints: deque, now: float) -> dict:
    """Aggression over the trailing window: who is crossing the spread?

    The single most-cited discretionary entry input, and absent from every
    free historical dataset.
    """
    recent = [p for p in prints if now - p["t"] <= TAPE_WINDOW]
    if not recent:
        return {"tape_n": 0, "tape_buy_vol": 0.0, "tape_sell_vol": 0.0,
                "tape_imbalance": 0.0, "tape_vol": 0.0}
    buy = sum(p["v"] for p in recent if p["dir"] == "BUY")
    sell = sum(p["v"] for p in recent if p["dir"] == "SELL")
    tot = buy + sell
    return {
        "tape_n": len(recent), "tape_buy_vol": buy, "tape_sell_vol": sell,
        # +1 = all buying aggression, -1 = all selling.
        "tape_imbalance": (buy - sell) / tot if tot else 0.0,
        "tape_vol": tot,
    }


def run(stop_event=None) -> int:   # pragma: no cover - needs a live gateway
    """Capture loop. Pass a ``threading.Event`` to stop it cleanly.

    Runs either standalone (python -m app.capture) or as a daemon thread
    started by the web app, so launching the dashboard is enough -- there
    is nothing extra to remember each morning.
    """
    from moomoo import OpenQuoteContext, RET_OK, SubType, SysConfig

    from .data import moomoo_data as mm

    OUT_DIR.mkdir(exist_ok=True)
    SysConfig.set_all_thread_daemon(True)
    q = OpenQuoteContext(host=settings.moomoo_opend_host or "127.0.0.1",
                         port=settings.moomoo_opend_port)

    underlying = mm._us_code(settings.scalper_underlying)
    rows: list[dict] = []
    tape: dict[str, deque] = {}
    subscribed: set[str] = set()
    last_chain = 0.0
    codes: list[str] = []

    log.info("capture starting: %s -> %s/", underlying, OUT_DIR)
    try:
        q.subscribe([underlying], [SubType.QUOTE])
        while not (stop_event and stop_event.is_set()):
            now = time.time()

            # Refresh the ATM contract set every few minutes as spot drifts.
            if now - last_chain > 300:
                ret, snap = q.get_market_snapshot([underlying])
                if ret != RET_OK or not len(snap):
                    time.sleep(5)
                    continue
                spot = float(snap.iloc[0]["last_price"])
                ret, exp = q.get_option_expiration_date(code=underlying)
                if ret != RET_OK or not len(exp):
                    time.sleep(5)
                    continue
                fut = exp[exp["option_expiry_date_distance"] >= 0]
                day = fut.iloc[0]["strike_time"]
                ret, ch = q.get_option_chain(code=underlying, start=day, end=day)
                if ret == RET_OK and len(ch):
                    ch = ch.assign(d=(ch["strike_price"] - spot).abs())
                    codes = ch.nsmallest(6, "d")["code"].astype(str).tolist()
                    new = [c for c in codes if c not in subscribed]
                    if new:
                        q.subscribe(new, [SubType.ORDER_BOOK, SubType.TICKER])
                        subscribed.update(new)
                        log.info("watching %d contracts (spot %.2f)",
                                 len(subscribed), spot)
                last_chain = now

            ret, snap = q.get_market_snapshot([underlying])
            spot = float(snap.iloc[0]["last_price"]) if ret == RET_OK and len(snap) else 0.0

            for code in codes:
                ret, book = q.get_order_book(code, num=5)
                if ret != RET_OK or not isinstance(book, dict):
                    continue
                bids, asks = book.get("Bid") or [], book.get("Ask") or []
                if not bids or not asks:
                    continue

                # Pull recent prints and fold them into the tape window.
                ret, tk = q.get_rt_ticker(code, num=50)
                dq = tape.setdefault(code, deque(maxlen=500))
                if ret == RET_OK and len(tk):
                    for _, r in tk.iterrows():
                        dq.append({
                            "t": now, "v": float(r.get("volume") or 0),
                            "dir": str(r.get("ticker_direction") or ""),
                        })

                bid_px, bid_sz = float(bids[0][0]), float(bids[0][1])
                ask_px, ask_sz = float(asks[0][0]), float(asks[0][1])
                mid = (bid_px + ask_px) / 2
                depth_bid = sum(float(b[1]) for b in bids)
                depth_ask = sum(float(a[1]) for a in asks)
                rows.append({
                    "ts": datetime.utcnow(), "code": code, "spot": spot,
                    "bid": bid_px, "ask": ask_px, "mid": mid,
                    "spread": ask_px - bid_px,
                    "spread_pct": (ask_px - bid_px) / mid if mid else 0.0,
                    "bid_size": bid_sz, "ask_size": ask_sz,
                    # +1 = book stacked on the bid, -1 = stacked on the ask.
                    "book_imbalance": ((bid_sz - ask_sz) / (bid_sz + ask_sz)
                                       if bid_sz + ask_sz else 0.0),
                    "depth_bid": depth_bid, "depth_ask": depth_ask,
                    "depth_levels": min(len(bids), len(asks)),
                    **_tape_stats(dq, now),
                })

            if len(rows) >= FLUSH_EVERY:
                day = datetime.utcnow().strftime("%Y-%m-%d")
                path = OUT_DIR / f"micro_{day}.parquet"
                df = pd.DataFrame(rows)
                if path.exists():
                    df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
                df.to_parquet(path)
                log.info("flushed %d rows -> %s (total %d)",
                         len(rows), path.name, len(df))
                rows = []

            if stop_event:
                stop_event.wait(SNAPSHOT_SECS)
            else:
                time.sleep(SNAPSHOT_SECS)
    except KeyboardInterrupt:
        log.info("capture stopped")
    except Exception:
        # A capture crash must never take the dashboard with it.
        log.exception("capture loop failed; data up to now is saved")
    finally:
        if rows:
            day = datetime.utcnow().strftime("%Y-%m-%d")
            path = OUT_DIR / f"micro_{day}.parquet"
            df = pd.DataFrame(rows)
            if path.exists():
                df = pd.concat([pd.read_parquet(path), df], ignore_index=True)
            df.to_parquet(path)
            log.info("final flush: %d rows", len(rows))
        q.close()
    return 0


def main() -> int:   # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
