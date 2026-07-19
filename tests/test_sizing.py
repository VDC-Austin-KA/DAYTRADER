"""Sizing tests. The asymmetry is the point: wins never raise size."""
from __future__ import annotations

from app.trading import sizing


def test_winning_days_never_increase_size():
    """The documented failure mode: 3x size after wins. Must be impossible."""
    flat, _ = sizing.streak_multiplier([])
    after_one_win, _ = sizing.streak_multiplier([5000.0])
    after_streak, _ = sizing.streak_multiplier([9000.0, 8000.0, 7000.0, 6000.0])
    assert after_one_win <= flat == 1.0
    assert after_streak <= 1.0, "a winning streak must not raise the multiplier"


def test_losing_days_shrink_size_geometrically():
    one, _ = sizing.streak_multiplier([-500.0])
    two, _ = sizing.streak_multiplier([-500.0, -400.0])
    three, _ = sizing.streak_multiplier([-500.0, -400.0, -300.0])
    assert one > two > three
    assert one == sizing.LOSS_DAY_DECAY


def test_multiplier_has_a_floor():
    """A long drawdown must not size to zero and prevent recovery."""
    mult, _ = sizing.streak_multiplier([-100.0] * 25)
    assert mult == sizing.MIN_MULTIPLIER


def test_streak_resets_on_a_win():
    """Most-recent-first: a win at the front clears the losing streak."""
    mult, _ = sizing.streak_multiplier([200.0, -500.0, -400.0, -300.0])
    assert mult == 1.0


def test_contracts_scale_with_equity_not_results():
    small = sizing.contracts_for(5_000, 0.50, 0.35)
    big = sizing.contracts_for(50_000, 0.50, 0.35)
    assert big.contracts > small.contracts
    # Same equity, opposite recent results -> winner is never larger.
    won = sizing.contracts_for(50_000, 0.50, 0.35, [9_000.0, 8_000.0])
    lost = sizing.contracts_for(50_000, 0.50, 0.35, [-900.0])
    assert won.contracts >= lost.contracts
    assert won.contracts == big.contracts, "wins must not add size"


def test_buying_power_is_a_hard_cap():
    """Risk budget may allow more than the account can actually fund."""
    d = sizing.contracts_for(50_000, 0.50, 0.35, buying_power=1_045.81)
    # 2/3 of $1,045.81 funds 13 contracts at $0.50; risk budget alone
    # would permit far more on $50k equity.
    assert d.contracts <= 13
    assert "buying power" in d.reason


def test_expensive_contract_yields_zero_not_a_gamble():
    d = sizing.contracts_for(500, 5.00, 0.35)
    assert d.contracts == 0 and "no trade" in d.reason


def test_march_scenario_would_have_been_capped():
    """After the +$86k day, the real account traded 962 contracts.

    Fixed-fractional on that equity, with wins barred from raising size,
    caps it at MAX_CONTRACTS -- two orders of magnitude smaller.
    """
    d = sizing.contracts_for(
        equity=89_649, entry_price=0.60, stop_pct=0.35,
        recent_daily_pnl=[86_143.0, 2_240.0],
    )
    assert d.contracts <= sizing.MAX_CONTRACTS
    assert d.multiplier == 1.0
