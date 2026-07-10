"""Unit tests for the vertical-spread bot's pure logic (no network/broker)."""
from __future__ import annotations

import asyncio
import math
import time
from datetime import date, timedelta

import pytest

from app.spreads.chain import ChainStore
from app.spreads.config import SpreadsConfig
from app.spreads.execution import (
    PaperSpreadExecutor,
    SpreadRouter,
    limit_price,
    plan_entry_legs,
    quotes_fresh,
)
from app.spreads.ivrank import IVRankTracker
from app.spreads.models import (
    OptionContract,
    Quote,
    Right,
    SpreadCandidate,
    SpreadKind,
    SpreadPosition,
)
from app.spreads.scanner import SpreadScanner

TODAY = date(2026, 7, 10)
EXPIRY = TODAY + timedelta(days=1)


def make_config(**overrides) -> SpreadsConfig:
    return SpreadsConfig(**overrides)


def contract(strike: float, right: Right = Right.PUT) -> OptionContract:
    return OptionContract(root="SPY", expiry=EXPIRY, right=right, strike=strike)


def build_chain(spot: float = 620.0) -> ChainStore:
    """Synthetic-but-sane 1DTE SPY put/call ladder around spot=620."""
    chain = ChainStore("SPY")
    now_ns = time.time_ns()
    # (strike, put_delta, put_mid) — deltas shrink walking away from money.
    puts = [
        (610, -0.10, 0.55), (612, -0.14, 0.75), (614, -0.20, 1.00),
        (616, -0.28, 1.40), (618, -0.38, 2.00), (620, -0.50, 2.80),
    ]
    calls = [
        (620, 0.50, 2.80), (622, 0.38, 2.00), (624, 0.28, 1.40),
        (626, 0.20, 1.00), (628, 0.14, 0.75), (630, 0.10, 0.55),
    ]
    for strike, delta, mid in puts + calls:
        right = Right.PUT if delta < 0 else Right.CALL
        c = contract(strike, right)
        chain.update_quote(c, bid=mid - 0.03, ask=mid + 0.03, ts_ns=now_ns)
        chain.update_greeks(c, delta=delta, gamma=0.02, iv=0.25)
    return chain


# --------------------------------------------------------------------------- #
# Symbols
# --------------------------------------------------------------------------- #
def test_occ_round_trip_and_broker_codes():
    c = OptionContract.from_occ("O:SPY260711P00614000")
    assert c == contract(614.0)
    assert c.occ_symbol == "SPY260711P00614000"
    assert c.polygon_ticker == "O:SPY260711P00614000"
    assert c.moomoo_code == "US.SPY260711P614000"
    assert c.dte(TODAY) == 1


def test_occ_rejects_garbage():
    with pytest.raises(ValueError):
        OptionContract.from_occ("SPY-not-an-option")


# --------------------------------------------------------------------------- #
# Chain store
# --------------------------------------------------------------------------- #
def test_chain_quote_update_and_lookup():
    chain = build_chain()
    q = chain.quote(contract(614))
    assert q is not None
    assert q.mid == pytest.approx(1.00)
    assert q.delta == pytest.approx(-0.20)
    assert chain.expiries(0, 3, TODAY) == [EXPIRY]
    ladder = chain.quotes_for(EXPIRY, Right.PUT)
    assert [x.contract.strike for x in ladder] == [610, 612, 614, 616, 618, 620]


def test_chain_atm_iv_averages_both_rights():
    chain = build_chain()
    assert chain.atm_iv(620.0) == pytest.approx(0.25)


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #
def test_high_iv_rank_selects_credit_spread_short_leg_at_target_delta():
    cfg = make_config(wing_width_strikes=2, min_dte=0, max_dte=3)
    cand = SpreadScanner(cfg).scan(build_chain(), iv_rank=85.0, spot=620.0, today=TODAY)
    assert cand is not None
    assert cand.kind is SpreadKind.CREDIT
    assert abs(abs(cand.short_leg.delta) - cfg.short_leg_delta) <= 0.10
    # Long wing strictly further OTM by the configured width.
    assert cand.width == pytest.approx(4.0)
    assert cand.net_mid > 0
    assert cand.max_risk_per_spread == pytest.approx((cand.width - cand.net_mid) * 100)


def test_low_iv_rank_selects_debit_spread_long_leg_nearer_money():
    cfg = make_config(wing_width_strikes=2, min_dte=0, max_dte=3)
    cand = SpreadScanner(cfg).scan(build_chain(), iv_rank=10.0, spot=620.0, today=TODAY)
    assert cand is not None
    assert cand.kind is SpreadKind.DEBIT
    assert cand.net_mid < 0  # pays a debit
    # The bought leg is closer to the money than the sold wing.
    if cand.long_leg.contract.right is Right.PUT:
        assert cand.long_leg.contract.strike > cand.short_leg.contract.strike
    else:
        assert cand.long_leg.contract.strike < cand.short_leg.contract.strike


def test_mid_iv_rank_stands_down():
    cfg = make_config()
    assert SpreadScanner(cfg).scan(build_chain(), iv_rank=50.0, spot=620.0, today=TODAY) is None


def test_wide_markets_are_rejected():
    cfg = make_config(max_quote_spread_pct=0.01)  # our ladder quotes 2-11% wide
    assert SpreadScanner(cfg).scan(build_chain(), iv_rank=85.0, spot=620.0, today=TODAY) is None


# --------------------------------------------------------------------------- #
# Pricing + staleness guardrail
# --------------------------------------------------------------------------- #
def test_limit_prices_bracket_mid_with_slippage():
    assert limit_price(1.00, "BUY", 0.01) == pytest.approx(1.01)
    assert limit_price(1.00, "SELL", 0.01) == pytest.approx(0.99)
    assert limit_price(0.001, "SELL", 0.01) == 0.01  # floor at a penny


def test_staleness_guardrail_150ms():
    now = time.time_ns()
    fresh = Quote(contract=contract(614), bid=1, ask=1.1, ts_ns=now - 50_000_000)
    stale = Quote(contract=contract(616), bid=1, ask=1.1, ts_ns=now - 200_000_000)
    never = Quote(contract=contract(618), bid=1, ask=1.1, ts_ns=0)
    assert quotes_fresh([fresh], 150, now)
    assert not quotes_fresh([fresh, stale], 150, now)
    assert not quotes_fresh([never], 150, now)  # snapshot seeds never tradeable


def test_entry_legs_buy_long_first():
    cfg = make_config()
    cand = SpreadScanner(cfg).scan(build_chain(), iv_rank=85.0, spot=620.0, today=TODAY)
    legs = plan_entry_legs(cand, 0.01)
    assert [l.side for l in legs] == ["BUY", "SELL"]
    assert legs[0].contract == cand.long_leg.contract


def test_router_aborts_on_stale_ticks():
    cfg = make_config(max_tick_staleness_ms=150)
    chain = build_chain()
    cand = SpreadScanner(cfg).scan(chain, iv_rank=85.0, spot=620.0, today=TODAY)
    # Age every tick far past the guardrail.
    for sl in chain._slices.values():
        sl.data[:, :, 5] = time.time_ns() - 1_000_000_000
    router = SpreadRouter(cfg, chain, PaperSpreadExecutor())
    assert asyncio.run(router.open_spread(cand)) is None


def test_router_fills_with_fresh_ticks():
    cfg = make_config(max_tick_staleness_ms=150)
    chain = build_chain()
    cand = SpreadScanner(cfg).scan(chain, iv_rank=85.0, spot=620.0, today=TODAY)
    router = SpreadRouter(cfg, chain, PaperSpreadExecutor())
    pos = asyncio.run(router.open_spread(cand))
    assert pos is not None
    assert pos.entry_price > 0  # net credit posted
    assert all(f.ok for f in pos.fills)


# --------------------------------------------------------------------------- #
# Position risk math
# --------------------------------------------------------------------------- #
def test_unrealized_loss_credit_and_debit():
    cfg = make_config()
    chain = build_chain()
    credit = SpreadScanner(cfg).scan(chain, iv_rank=85.0, spot=620.0, today=TODAY)
    pos = SpreadPosition(candidate=credit, entry_price=0.45)
    assert pos.unrealized_loss(0.45) == 0.0
    assert pos.unrealized_loss(0.95) == pytest.approx(50.0)   # credit widened against us
    assert pos.unrealized_loss(0.10) == 0.0                    # winning, not a loss

    debit = SpreadScanner(cfg).scan(chain, iv_rank=10.0, spot=620.0, today=TODAY)
    dpos = SpreadPosition(candidate=debit, entry_price=-1.00)
    assert dpos.unrealized_loss(-0.40) == pytest.approx(60.0)  # owned spread shrank
    assert dpos.unrealized_loss(-1.60) == 0.0                  # winning


# --------------------------------------------------------------------------- #
# Watchdog circuit breakers
# --------------------------------------------------------------------------- #
def test_watchdog_position_stop_flattens_the_spread():
    from app.spreads.watchdog import RiskWatchdog

    cfg = make_config(position_stop_pct_of_max_risk=0.50)
    chain = build_chain()
    cand = SpreadScanner(cfg).scan(chain, iv_rank=85.0, spot=620.0, today=TODAY)
    router = SpreadRouter(cfg, chain, PaperSpreadExecutor())
    watchdog = RiskWatchdog(cfg, chain, router)
    pos = asyncio.run(router.open_spread(cand))
    watchdog.positions.append(pos)

    # Blow the short leg's mid out so the adverse move exceeds 50% of max risk.
    short = cand.short_leg.contract
    max_risk = cand.max_risk_per_spread
    bad_mid = pos.entry_price + cand.long_leg.mid + (0.6 * max_risk / 100.0)
    chain.update_quote(short, bid=bad_mid - 0.05, ask=bad_mid + 0.05, ts_ns=time.time_ns())

    asyncio.run(watchdog.check())
    assert pos.closed
    assert not watchdog.halted  # single stop, not an account breaker


def test_watchdog_daily_equity_breaker_halts_and_flattens():
    from app.spreads.watchdog import RiskWatchdog

    cfg = make_config(daily_equity_stop_pct=0.03, paper_starting_equity=100000)
    chain = build_chain()
    cand = SpreadScanner(cfg).scan(chain, iv_rank=85.0, spot=620.0, today=TODAY)
    router = SpreadRouter(cfg, chain, PaperSpreadExecutor())
    watchdog = RiskWatchdog(cfg, chain, router)
    pos = asyncio.run(router.open_spread(cand))
    watchdog.positions.append(pos)
    # Pretend the session started much richer -> current equity breaches -3%.
    watchdog.session_start_equity = 200000.0

    asyncio.run(watchdog.check())
    assert watchdog.halted
    assert pos.closed
    assert "circuit breaker" in watchdog.halt_reason


# --------------------------------------------------------------------------- #
# IV rank
# --------------------------------------------------------------------------- #
def test_iv_rank_needs_history_then_ranks(tmp_path):
    tracker = IVRankTracker(str(tmp_path / "iv.json"), window_days=30)
    assert tracker.rank(0.20) is None  # cold start: no regime call
    t0 = time.time() - 86400
    for i in range(20):
        tracker.observe(0.15 + i * 0.005, now=t0 + i * 600)  # 0.15 .. 0.245
    assert tracker.rank(0.245) == pytest.approx(100.0, abs=1e-6)
    assert tracker.rank(0.15) == pytest.approx(0.0, abs=1e-6)
    mid_rank = tracker.rank(0.1975)
    assert 45 < mid_rank < 55


def test_iv_rank_persists_across_restart(tmp_path):
    path = str(tmp_path / "iv.json")
    first = IVRankTracker(path, window_days=30)
    t0 = time.time() - 3600
    for i in range(15):
        first.observe(0.20 + i * 0.01, now=t0 + i * 301)
    first._save()
    reloaded = IVRankTracker(path, window_days=30)
    assert reloaded.rank(0.34) is not None


def test_iv_rank_ignores_nan_marks(tmp_path):
    tracker = IVRankTracker(str(tmp_path / "iv.json"), window_days=30)
    tracker.observe(float("nan"))
    tracker.observe(0.0)
    assert tracker._samples == []
