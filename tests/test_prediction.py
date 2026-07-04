"""Offline tests for the BTC hourly prediction bot (no network).

app.models is imported lazily (inside tests/fixtures) so test_core's
database-reload pattern keeps working regardless of collection order.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.prediction import model as pmodel


def _returns(n: int = 400, mean: float = 0.0, std: float = 0.0006, seed: int = 3):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mean, std, n))


# --- Probability model ---

def test_probability_bounds_and_atm():
    est = pmodel.estimate_p_above(
        spot=100_000, strike=100_000, minutes_remaining=30, minute_returns=_returns()
    )
    assert 0.01 <= est.p_above <= 0.99
    # At the money with negligible drift the probability is near a coin flip.
    assert 0.35 <= est.p_above <= 0.65


def test_probability_monotonic_in_strike():
    r = _returns()
    ps = [
        pmodel.estimate_p_above(100_000, k, 30, r).p_above
        for k in (99_000, 100_000, 101_000)
    ]
    assert ps[0] > ps[1] > ps[2]


def test_probability_sharpens_near_settlement():
    r = _returns()
    far = pmodel.estimate_p_above(100_000, 100_300, 55, r).p_above
    near = pmodel.estimate_p_above(100_000, 100_300, 2, r).p_above
    # Same distance above spot becomes less likely as time runs out.
    assert near < far


def test_probability_handles_empty_returns():
    est = pmodel.estimate_p_above(100_000, 100_050, 30, pd.Series(dtype=float))
    assert 0.01 <= est.p_above <= 0.99
    assert est.sigma_per_min == pmodel.DEFAULT_SIGMA_PER_MIN


# --- Sizing ---

def test_kelly_sizing_caps():
    from app.config import settings
    from app.prediction import risk as prisk

    contracts, stake = prisk.size_position(bankroll=1000, p_win=0.70, cost=0.50)
    assert contracts >= 1
    assert stake <= settings.prediction_max_stake_usd
    assert stake <= settings.prediction_max_stake_pct * 1000 + 1e-9


def test_no_size_without_edge():
    from app.prediction import risk as prisk

    assert prisk.size_position(1000, p_win=0.40, cost=0.50) == (0, 0.0)
    assert prisk.size_position(0, p_win=0.90, cost=0.50) == (0, 0.0)
    assert prisk.size_position(1000, p_win=0.90, cost=1.0) == (0, 0.0)


# --- Risk gates + settlement (isolated SQLite DB) ---

@pytest.fixture()
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models  # noqa: F401  (register tables on Base)
    from app.database import Base

    engine = create_engine(
        f"sqlite:///{tmp_path}/p.db", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _trade(db, *, pnl=0.0, result="", status="settled", side="yes",
           strike=100_000.0, close_offset_min=-90, settled_at=None):
    from app.models import PredictionTrade

    now = datetime.utcnow()
    row = PredictionTrade(
        ticker=f"KXBTCD-T{strike}-{np.random.default_rng().integers(10**9)}",
        series="KXBTCD",
        side=side,
        strike=strike,
        close_time=now + timedelta(minutes=close_offset_min),
        probability=0.6,
        entry_price=0.5,
        quantity=10,
        stake=5.0,
        status=status,
        result=result,
        pnl=pnl,
        settled_at=settled_at or (now if status == "settled" else None),
    )
    db.add(row)
    db.commit()
    return row


def test_daily_loss_halts(db):
    from app.config import settings
    from app.prediction import risk as prisk

    _trade(db, pnl=-(settings.prediction_max_daily_loss + 1), result="loss")
    decision = prisk.check(db, datetime.utcnow())
    assert not decision.allowed
    assert "daily loss" in decision.reason


def test_consecutive_loss_cooldown(db):
    from app.config import settings
    from app.prediction import risk as prisk

    for _ in range(settings.prediction_max_consecutive_losses):
        _trade(db, pnl=-1.0, result="loss")
    decision = prisk.check(db, datetime.utcnow())
    assert not decision.allowed
    assert "consecutive losses" in decision.reason


def test_max_open_positions(db):
    from app.config import settings
    from app.prediction import risk as prisk

    for _ in range(settings.prediction_max_open):
        _trade(db, status="open", result="")
    decision = prisk.check(db, datetime.utcnow())
    assert not decision.allowed
    assert "open positions" in decision.reason


def test_bankroll_tracks_settled_pnl(db):
    from app.config import settings
    from app.prediction import risk as prisk

    _trade(db, pnl=25.0, result="win")
    _trade(db, pnl=-10.0, result="loss")
    assert prisk.current_bankroll(db) == settings.prediction_bankroll + 15.0


def test_settlement_win_and_loss(db, monkeypatch):
    from app.prediction import bot

    yes = _trade(db, status="open", result="", side="yes", strike=100_000.0)
    no = _trade(db, status="open", result="", side="no", strike=110_000.0)
    monkeypatch.setattr(bot.data, "price_at", lambda when: 105_000.0)

    n = bot.settle_due_trades(db)
    assert n == 2
    db.refresh(yes)
    db.refresh(no)
    # BTC settled above the YES strike and below the NO strike: both win.
    assert yes.result == "win" and yes.pnl == pytest.approx(10 * 0.5)
    assert no.result == "win" and no.pnl == pytest.approx(10 * 0.5)


# --- Market selection ---

def test_select_hourly_market(monkeypatch):
    from app.prediction import markets as pmkts

    now = datetime.now(timezone.utc)
    close = (now + timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    later = (now + timedelta(minutes=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fixture = [
        {"ticker": "NEAR-ATM", "strike_type": "greater", "floor_strike": 100_100,
         "close_time": close},
        {"ticker": "FAR-STRIKE", "strike_type": "greater", "floor_strike": 120_000,
         "close_time": close},
        {"ticker": "NEXT-HOUR", "strike_type": "greater", "floor_strike": 100_000,
         "close_time": later},
        {"ticker": "RANGE", "strike_type": "between", "floor_strike": 99_000,
         "cap_strike": 101_000, "close_time": close},
    ]
    detail = {
        "ticker": "NEAR-ATM",
        "yes_bid_dollars": "0.4000", "yes_ask_dollars": "0.4500",
        "no_bid_dollars": "0.5500", "no_ask_dollars": "0.6000",
    }
    monkeypatch.setattr(pmkts, "fetch_open_markets", lambda series=None: fixture)
    monkeypatch.setattr(pmkts, "get_market", lambda ticker: detail)
    pick = pmkts.select_hourly_market(spot=100_000.0)
    assert pick is not None
    assert pick.ticker == "NEAR-ATM"
    assert pick.yes_ask == pytest.approx(0.45)
    assert pick.no_ask == pytest.approx(0.60)
    assert 0 < pick.minutes_remaining() <= 41


def test_price_parsing_falls_back_to_cents():
    from app.prediction.markets import _price

    assert _price({"yes_ask_dollars": "0.5150"}, "yes_ask") == pytest.approx(0.515)
    assert _price({"yes_ask": 45}, "yes_ask") == pytest.approx(0.45)
    assert _price({}, "yes_ask") == 0.0
