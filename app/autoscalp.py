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

  ENTRIES (burst rule + condition gate): when the underlying moves >=
  ``entry_burst`` in under ``burst_window`` seconds, a burst is proposed in
  the move's direction. Before it becomes an order the REAL-TIME CONDITION
  GATE (app/trading/conditions.py) scores the tape the burst fired into and
  refuses it in chop (low Kaufman efficiency) or against a decisive trend
  (a call into a slide, a put into a rip) -- the two regimes this repo's own
  backtests tie to the losses. A burst that clears the gate buys the ATM
  contract (call on up, put on down). One position at a time; cooldown
  between entries; all session rules apply (no entries after 14:00 CT,
  flatten 14:45 CT, liquidity gates, buying-power cap).

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
# Daily circuit breaker: expressed as a FRACTION of live buying power so
# it scales with the account instead of drifting out of proportion. Once
# today's realized P&L is at or below -(fraction * buying power), no more
# entries until tomorrow. Exits keep running -- the breaker stops digging,
# it never blocks getting out.
MAX_DAILY_LOSS_FRAC = settings.scalper_max_daily_loss_frac
MAX_DAILY_LOSS_FLOOR = settings.scalper_max_daily_loss   # absolute backstop

# Real-time condition gate (see app/trading/conditions.py).
CONDITION_GATE = settings.scalper_condition_gate
TREND_WINDOW = settings.scalper_trend_window
MIN_EFFICIENCY = settings.scalper_min_efficiency
OPPOSE_TREND_BPS = settings.scalper_oppose_trend_bps


def daily_loss_limit(buying_power: float) -> float:
    """Today's loss budget in dollars. Never exceeds the absolute floor."""
    if buying_power <= 0:
        return MAX_DAILY_LOSS_FLOOR
    return min(buying_power * MAX_DAILY_LOSS_FRAC, MAX_DAILY_LOSS_FLOOR)


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
_trend_path: deque = deque()         # (ts, price) for the condition gate


def _record_spot(price: float) -> None:
    now = time.time()
    CACHE.spot, CACHE.spot_ts = price, now
    _spot_path.append((now, price))
    cutoff = now - BURST_WINDOW
    while _spot_path and _spot_path[0][0] < cutoff:
        _spot_path.popleft()
    # A longer window for the condition gate: it judges the regime the burst
    # fired into, which needs more context than the burst detector's window.
    _trend_path.append((now, price))
    tcut = now - TREND_WINDOW
    while _trend_path and _trend_path[0][0] < tcut:
        _trend_path.popleft()


def _recent_daily_pnl(db, pf, days: int = 10) -> list[float]:
    """Realized P&L per day, most recent first, for the sizing multiplier."""
    from sqlalchemy import func

    from .models import Trade

    rows = (
        db.query(
            func.date(Trade.timestamp).label("d"),
            func.coalesce(func.sum(Trade.realized_pnl), 0.0).label("pnl"),
        )
        .filter(Trade.portfolio_id == pf.id, Trade.side == "sell")
        .group_by("d")
        .order_by(func.date(Trade.timestamp).desc())
        .limit(days)
        .all()
    )
    return [float(r.pnl or 0.0) for r in rows]


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
    from .trading import brackets, conditions, notify, paper, scalp
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

            # Roll the expiry each calendar day. The daemon runs overnight,
            # so an expiry pinned at startup goes stale by morning -- which
            # made every entry target a dead contract and get blocked. Re-read
            # it and drop the previous day's subscribed contracts.
            today = sess.now_ct().strftime("%Y-%m-%d")
            if today != expiry:
                log.info("rolling expiry %s -> %s", expiry, today)
                expiry = today
                contracts.clear()
                last_resub = 0.0

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
                        if ok:
                            pnl = (act.est_price - st.entry_price) * act.sell_qty * 100
                            notify.record(
                                "exit",
                                f"SOLD {act.sell_qty} x {pos.contract_symbol} "
                                f"({act.kind})",
                                f"@ ${act.est_price:.2f}, P&L ${pnl:+,.2f}. "
                                f"{act.reason}",
                                level="trade", code=pos.contract_symbol,
                                qty=act.sell_qty, price=act.est_price, pnl=pnl)
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
                    from .trading import moomoo_account as _ma

                    _acct = _ma.account_summary()
                    _bp = (float(_acct.get("us_buying_power") or 0)
                           if _acct.get("ok") else 0.0)
                    limit = daily_loss_limit(_bp)
                    if realized <= -limit:
                        can = False
                        notify.record(
                            "blocked", "Daily loss limit hit",
                            f"today {realized:+.2f} <= -{limit:.2f} "
                            f"({MAX_DAILY_LOSS_FRAC:.0%} of ${_bp:,.0f} buying "
                            "power). No more entries today; exits still run.",
                            level="warn")
                        log.warning(
                            "circuit breaker: today %.2f <= -%.2f; no more "
                            "entries today", realized, limit)
                if (can and not open_pos and now - last_entry > COOLDOWN):
                    direction = detect_burst()
                    # Real-time condition gate: a burst is necessary but not
                    # sufficient. Refuse it in chop or against a decisive
                    # trend -- the two regimes the backtests tie to the losses.
                    if direction and CONDITION_GATE:
                        assessment = conditions.assess(
                            list(_trend_path), direction,
                            min_efficiency=MIN_EFFICIENCY,
                            oppose_trend_bps=OPPOSE_TREND_BPS)
                        if not assessment.tradeable:
                            log.info("entry gated (%s-burst): %s",
                                     direction, assessment.describe())
                            direction = None
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
                            # Size from equity and recent DAILY results --
                            # never from conviction, and never larger after
                            # a win. This is the rule the 702-trip history
                            # says the account actually needed.
                            from .trading import moomoo_account, sizing

                            acct = moomoo_account.account_summary()
                            bp = (float(acct.get("us_buying_power") or 0)
                                  if acct.get("ok") else 0.0)
                            equity = float(acct.get("us_assets") or 0) or bp
                            decision = sizing.contracts_for(
                                equity=equity, entry_price=ask,
                                stop_pct=brackets.STOP_LOSS_PCT,
                                recent_daily_pnl=_recent_daily_pnl(db, pf),
                                buying_power=bp,
                                bp_fraction=settings.buying_power_fraction,
                                max_contracts=QTY,
                            )
                            if decision.contracts <= 0:
                                log.info("entry skipped: %s", decision.reason)
                            else:
                                pos, msg = paper.open_position(
                                    db, pf, UNDERLYING, meta["right"], code,
                                    meta["strike"], expiry,
                                    decision.contracts, ask,
                                    note=f"autoscalp burst-{direction}")
                                log.warning("ENTRY %s %s %s @ %.2f -> %s",
                                            direction, code,
                                            decision.describe(), ask, msg)
                                if pos is not None:
                                    notify.record(
                                        "entry",
                                        f"BOUGHT {decision.contracts} x {code}",
                                        f"{direction}-burst @ ${ask:.2f} "
                                        f"(cost ${ask*decision.contracts*100:,.2f}). "
                                        f"{decision.reason}",
                                        level="trade", code=code,
                                        qty=decision.contracts, price=ask)
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
