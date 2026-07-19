"""Push-driven autonomous 0DTE scalper.  Run:  python -m app.autoscalp

WHY PUSH, NOT POLLING
---------------------
OpenD supports subscriptions: after one ``subscribe()`` call it STREAMS
every quote change to a local callback, millisecond-latency, no request
budget spent. This is the fastest free market data available to this
account -- Polygon's free tier is 5 calls/min delayed, Tradier's sandbox is
delayed, and both lose to a local push socket. The 10s bracket_monitor in
the scheduler stays as a belt-and-braces fallback; this daemon reacts to
the tick itself.

WHAT IT AUTOMATES
-----------------
The user's own demonstrated playbook (2026-07-16/17 tape, +$1,938 over two
days): ATM SPY 0DTE, fast cycles, cut losses immediately -- now with the
scale-out/trail exit from app/trading/brackets.py instead of all-out
paper-hands exits.

  ENTRIES (burst rule): when the underlying moves >= ``entry_burst`` in
  under ``burst_window`` seconds, buy the ATM contract in the move's
  direction (call on up-burst, put on down-burst). One position at a time;
  cooldown between entries; all session rules apply (no entries after
  14:00 CT, flatten 14:45 CT, liquidity gates, buying-power cap).

  EXITS: every pushed tick runs the bracket state machine. Bank half at
  +75%, trail the rest 25% off the high, hard-stop at -35%.

MODE -- READ THIS
-----------------
``SCALPER_TRADE_MODE`` defaults to **paper**. The exit discipline is
validated against the user's real tape; the ENTRY rule is not validated by
any backtest (the burst pattern matched their winning days, but two days
is a sample, not evidence). Paper mode runs the full loop against live
pushed prices and records what it would have done; flip to live only after
the paper record earns it.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from .config import settings

log = logging.getLogger("daytrader.autoscalp")

# --- Tunables ------------------------------------------------------------
# Read from settings, NOT os.getenv: pydantic-settings parses .env but does
# not populate os.environ, so module-level os.getenv silently ignored every
# value set in .env -- including the paper/live switch, which would have
# stayed "paper" while the file said "live".
UNDERLYING = settings.scalper_underlying
ENTRY_BURST = settings.scalper_entry_burst
BURST_WINDOW = settings.scalper_burst_window
COOLDOWN = settings.scalper_cooldown
QTY = settings.scalper_qty
TRADE_MODE = settings.scalper_trade_mode
# Daily circuit breaker: once today's realized P&L is at or below -this,
# no more entries until tomorrow. Exits keep running -- the breaker stops
# digging, it never blocks getting out.
MAX_DAILY_LOSS = settings.scalper_max_daily_loss
RESUBSCRIBE_SECS = 60.0        # refresh ATM strikes this often


@dataclass
class TickCache:
    """Latest pushed bid/ask per contract code, written by the handler
    thread, read by the trading loop. A dict assignment is atomic under the
    GIL, so no lock is needed for single-key reads/writes."""
    bids: dict = None
    asks: dict = None
    spot: float = 0.0
    spot_ts: float = 0.0

    def __post_init__(self):
        self.bids, self.asks = {}, {}


CACHE = TickCache()
_spot_path: deque = deque()          # (ts, price) for the burst detector


def _record_spot(price: float) -> None:
    now = time.time()
    CACHE.spot, CACHE.spot_ts = price, now
    _spot_path.append((now, price))
    cutoff = now - BURST_WINDOW
    while _spot_path and _spot_path[0][0] < cutoff:
        _spot_path.popleft()


def detect_burst() -> str | None:
    """'up' / 'down' if the underlying moved ENTRY_BURST inside the window."""
    if len(_spot_path) < 2:
        return None
    first, last = _spot_path[0][1], _spot_path[-1][1]
    if not first:
        return None
    move = last / first - 1.0
    if move >= ENTRY_BURST:
        return "up"
    if move <= -ENTRY_BURST:
        return "down"
    return None


def _atm_contracts(quote_ctx, expiry: str) -> dict[str, dict]:
    """{code: {strike, right, bid, ask}} for ATM +/-1 strikes, 0DTE."""
    from moomoo import RET_OK

    from .data import moomoo_data as mm

    ret, chain = quote_ctx.get_option_chain(
        code=mm._us_code(UNDERLYING), start=expiry, end=expiry
    )
    if ret != RET_OK or chain is None or not len(chain):
        return {}
    spot = CACHE.spot
    if not spot:
        return {}
    strikes = sorted(chain["strike_price"].unique(), key=lambda k: abs(k - spot))
    keep = set(strikes[:3])          # ATM and the strike either side
    out = {}
    for _, r in chain.iterrows():
        if r["strike_price"] in keep:
            out[str(r["code"])] = {
                "strike": float(r["strike_price"]),
                "right": "call" if "CALL" in str(r["option_type"]).upper() else "put",
            }
    return out


class _Handlers:
    """moomoo push callbacks -> TickCache. Built lazily so the moomoo import
    stays inside main() and the module can be imported in tests without it."""

    @staticmethod
    def build():
        from moomoo import OrderBookHandlerBase, RET_OK, StockQuoteHandlerBase

        class QuoteHandler(StockQuoteHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == RET_OK and len(data):
                    for _, r in data.iterrows():
                        if str(r["code"]).endswith(UNDERLYING):
                            _record_spot(float(r["last_price"]))
                return ret, data

        class BookHandler(OrderBookHandlerBase):
            def on_recv_rsp(self, rsp_pb):
                ret, data = super().on_recv_rsp(rsp_pb)
                if ret == RET_OK and isinstance(data, dict):
                    code = str(data.get("code", ""))
                    bids, asks = data.get("Bid") or [], data.get("Ask") or []
                    if bids:
                        CACHE.bids[code] = float(bids[0][0])
                    if asks:
                        CACHE.asks[code] = float(asks[0][0])
                return ret, data

        return QuoteHandler(), BookHandler()


def main() -> int:   # pragma: no cover - needs a live gateway
    from moomoo import OpenQuoteContext, RET_OK, SubType, SysConfig

    from .data import moomoo_data as mm
    from .database import SessionLocal, init_db
    from .trading import brackets, paper, scalp
    from .trading import session as sess

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    init_db()
    log.info("autoscalp starting: %s mode=%s qty=%d burst=%.4f/%.0fs",
             UNDERLYING, TRADE_MODE, QTY, ENTRY_BURST, BURST_WINDOW)
    if TRADE_MODE != "paper" and settings.dashboard_trade_mode != "moomoo":
        log.error("live mode needs DASHBOARD_TRADE_MODE=moomoo; refusing.")
        return 1

    SysConfig.set_all_thread_daemon(True)
    q = OpenQuoteContext(host=settings.moomoo_opend_host or "127.0.0.1",
                         port=settings.moomoo_opend_port)
    qh, bh = _Handlers.build()
    q.set_handler(qh)
    q.set_handler(bh)

    expiry = sess.now_ct().strftime("%Y-%m-%d")
    contracts: dict[str, dict] = {}
    states: dict[int, brackets.BracketState] = {}
    last_entry = 0.0
    last_resub = 0.0

    # Prime the spot + subscribe the underlying.
    q.subscribe([mm._us_code(UNDERLYING)], [SubType.QUOTE], subscribe_push=True)

    try:
        while True:
            now = time.time()

            # Refresh the ATM contract set periodically (spot drifts).
            if now - last_resub > RESUBSCRIBE_SECS and CACHE.spot:
                fresh = _atm_contracts(q, expiry)
                new_codes = [c for c in fresh if c not in contracts]
                if new_codes:
                    q.subscribe(new_codes, [SubType.ORDER_BOOK],
                                subscribe_push=True)
                    log.info("subscribed %d contracts: %s",
                             len(new_codes), new_codes)
                contracts.update(fresh)
                last_resub = now

            db = SessionLocal()
            try:
                pf = paper.get_or_create_portfolio(db)
                open_pos = [p for p in pf.positions if p.status == "open"]

                # --- EXITS: run brackets on the freshest pushed bid.
                for pos in open_pos:
                    st = states.get(pos.id)
                    if st is None:
                        st = brackets.BracketState(
                            position_id=pos.id, entry_price=pos.entry_price,
                            quantity=pos.quantity)
                        states[pos.id] = st
                        log.info("bracket armed #%s: %s", pos.id, st.describe())
                    bid = CACHE.bids.get(pos.contract_symbol, 0.0)
                    act = brackets.check(st, bid)
                    if act.kind != "none":
                        ok, msg = paper.close_position(
                            db, pf, pos.id, price=bid,
                            quantity=act.sell_qty, note=f"autoscalp {act.kind}")
                        log.warning("%s #%s: %s -> %s",
                                    act.kind, pos.id, act.reason, msg)
                        if not ok:
                            st.closed = False
                            st.remaining += act.sell_qty
                            if act.kind == "scale_out":
                                st.scaled_out = False

                # --- ENTRIES: burst rule, one position at a time.
                can, _why = sess.can_open()
                if can and not open_pos and now - last_entry > COOLDOWN:
                    from sqlalchemy import func

                    from .models import Trade

                    today = sess.now_ct().date()
                    realized = float(db.query(
                        func.coalesce(func.sum(Trade.realized_pnl), 0.0)
                    ).filter(
                        Trade.portfolio_id == pf.id,
                        Trade.side == "sell",
                        func.date(Trade.timestamp) == today.isoformat(),
                    ).scalar() or 0.0)
                    if realized <= -MAX_DAILY_LOSS:
                        can = False
                        log.warning(
                            "circuit breaker: today %.2f <= -%.2f; no more "
                            "entries today", realized, MAX_DAILY_LOSS)
                if (can and not open_pos and now - last_entry > COOLDOWN):
                    direction = detect_burst()
                    if direction:
                        right = "call" if direction == "up" else "put"
                        pick = None
                        for code, meta in contracts.items():
                            if meta["right"] != right:
                                continue
                            bid = CACHE.bids.get(code, 0)
                            ask = CACHE.asks.get(code, 0)
                            if not bid or not ask:
                                continue
                            c = {"contractSymbol": code, "bid": bid, "ask": ask,
                                 "openInterest": 10_000, "volume": 10_000,
                                 "strike": meta["strike"]}
                            ok_liq, _ = scalp.check_liquidity(c)
                            if ok_liq and (pick is None or
                                           abs(meta["strike"] - CACHE.spot) <
                                           abs(pick[1]["strike"] - CACHE.spot)):
                                pick = (code, meta, ask)
                        if pick:
                            code, meta, ask = pick
                            pos, msg = paper.open_position(
                                db, pf, UNDERLYING, meta["right"], code,
                                meta["strike"], expiry, QTY, ask,
                                note=f"autoscalp burst-{direction}")
                            log.warning("ENTRY %s %s x%d @ %.2f -> %s",
                                        direction, code, QTY, ask, msg)
                            last_entry = now
            finally:
                db.close()

            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("autoscalp stopped by user")
    finally:
        q.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
