"""Tests for VIDAR sizing — cap enforcement, math correctness.

Reference: vidar.idx caps:
  max_contracts_per_trade: 1
  max_loss_per_trade_usd: 500.0
"""
import sys
sys.path.insert(0, "/home/freya/vidar")

from vidar_pkg.sizing import size_spy_ic, VidarSizing


def test_basic_sizing():
    plan = size_spy_ic(
        underlying="SPY",
        short_put_strike=741.0, long_put_strike=736.0,
        short_call_strike=802.0, long_call_strike=807.0,
        net_credit=1.09, quantity=1,
    )
    assert plan.underlying == "SPY"
    assert plan.quantity == 1
    assert plan.credit_per_share == 1.09
    assert plan.wing_width == 5.0
    assert abs(plan.max_loss_per_share - 3.91) < 0.001
    assert abs(plan.max_loss_total - 391.0) < 0.1
    assert abs(plan.max_profit_total - 109.0) < 0.1
    assert abs(plan.breakeven_lower - 739.91) < 0.01
    assert abs(plan.breakeven_upper - 803.09) < 0.01
    print("  ✓ basic sizing math correct")


def test_passes_caps_under():
    plan = size_spy_ic("SPY", 741, 736, 802, 807, 1.09)
    ok, reason = plan.passes_caps()
    assert ok, f"expected to pass caps, got reason: {reason}"
    print("  ✓ $391 max loss passes the $500 cap")


def test_passes_caps_over():
    plan = size_spy_ic("SPY", 741, 736, 802, 807, 0.50)  # very low credit
    # max loss = 5 - 0.50 = 4.50 → $450 total, still under $500
    ok, reason = plan.passes_caps()
    assert ok, f"$450 should still pass: {reason}"

    # Now try a wider wing
    plan2 = size_spy_ic("SPY", 741, 731, 802, 812, 1.50)  # $10 wings
    ok2, reason2 = plan2.passes_caps()
    assert not ok2, f"$850 max loss should fail"
    assert "$850" in reason2 or "$8" in reason2
    print("  ✓ cap refuses oversized positions with exact reason")


def test_wing_width_must_be_equal():
    import pytest
    try:
        size_spy_ic("SPY", 741, 736, 802, 815, 1.0)  # put wing=5, call wing=13
        assert False, "should have raised"
    except ValueError as e:
        assert "unequal" in str(e).lower()
    print("  ✓ rejects unequal wing widths")


def test_credit_cannot_exceed_wing():
    import pytest
    try:
        size_spy_ic("SPY", 741, 736, 802, 807, 6.0)  # credit > wing
        assert False, "should have raised"
    except ValueError as e:
        assert "wing" in str(e).lower()
    print("  ✓ rejects credit > wing width")


def test_short_strikes_must_be_inside_longs():
    import pytest
    try:
        size_spy_ic("SPY", 736, 741, 802, 807, 1.0)  # short_put < long_put
        assert False, "should have raised"
    except ValueError as e:
        assert "short_put_strike" in str(e)
    print("  ✓ validates strike ordering (short inside, long outside)")


if __name__ == "__main__":
    print("=" * 60)
    print("VIDAR sizing tests")
    print("=" * 60)
    test_basic_sizing()
    test_passes_caps_under()
    test_passes_caps_over()
    test_wing_width_must_be_equal()
    test_credit_cannot_exceed_wing()
    test_short_strikes_must_be_inside_longs()
    print("\nALL TESTS PASS")
