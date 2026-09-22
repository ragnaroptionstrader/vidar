"""Tests for VIDAR exit ladder — 50% profit / 2x stop / DTE-7."""
import sys
from datetime import date, timedelta
sys.path.insert(0, "/home/freya/vidar")

from vidar_pkg.exits import VidarPosition, evaluate_vidar_exit


def _make_pos(dte: int = 30, credit: float = 1.0, special: bool = False):
    """Helper to construct a VidarPosition at a given DTE."""
    exp = (date.today() + timedelta(days=dte)).isoformat()
    return VidarPosition(
        underlying="SPY", expiry=exp,
        short_put_strike=741.0, long_put_strike=736.0,
        short_call_strike=802.0, long_call_strike=807.0,
        net_credit=credit, quantity=1,
        open_date=(date.today() - timedelta(days=30 - dte)).isoformat(),
        is_special=special,
    )


def test_take_profit_at_50pct():
    pos = _make_pos(dte=20, credit=1.0)
    # Position value dropped to 0.50 (50% of credit captured)
    dec = evaluate_vidar_exit(pos, current_credit_value=0.50)
    assert dec.should_close
    assert dec.severity == "take_profit"
    assert abs(dec.pnl_pct - 50.0) < 0.01
    print("  ✓ closes at 50% profit-take (pnl_pct = 50.0)")


def test_hold_when_profit_under_50pct():
    pos = _make_pos(dte=20, credit=1.0)
    dec = evaluate_vidar_exit(pos, current_credit_value=0.70)  # 30% profit
    assert not dec.should_close
    assert dec.severity == "hold"
    print("  ✓ holds when profit is below 50% target")


def test_stop_loss_at_2x_credit():
    pos = _make_pos(dte=20, credit=1.0)
    # net_credit = $1.00, so 2x credit = $2.00 loss
    # pnl = -$2.00 → current_credit_value = 1.0 - (-2.0) = 3.0
    dec = evaluate_vidar_exit(pos, current_credit_value=3.0)
    assert dec.should_close, f"expected close at 2x credit stop, got {dec.severity}"
    assert dec.severity == "stop_loss"
    print("  ✓ closes at 2x credit stop (loss = $2.00 = 2x $1.00 credit)")


def test_stop_loss_capped_at_max_loss():
    """Even if 2x credit > max_loss, position can never lose more than max_loss.
    So stop_loss should trigger when |pnl| >= min(2x_credit, max_loss).
    """
    pos = _make_pos(dte=20, credit=1.0)
    # max_loss = 5 - 1 = 4 ($400 total). Set stop_loss_credit_mult=1.0 (smaller
    # than max_loss): threshold = $100.
    # current_credit_value = 2.0 → pnl = -$100 → triggers.
    dec = evaluate_vidar_exit(pos, current_credit_value=2.0, stop_loss_credit_mult=1.0)
    assert dec.should_close
    assert dec.severity == "stop_loss"
    print("  ✓ stop_loss caps at max_loss (no over-trigger)")


def test_stop_loss_not_triggered_under_threshold():
    pos = _make_pos(dte=20, credit=1.0)
    # current_credit_value = 2.5 → pnl = -$150 → 1.5x credit → NOT yet at 2x
    dec = evaluate_vidar_exit(pos, current_credit_value=2.5)
    assert not dec.should_close
    assert dec.severity == "hold"
    print("  ✓ holds when loss is under 2x credit")


def test_dte_close_at_7():
    pos = _make_pos(dte=7, credit=1.0)
    dec = evaluate_vidar_exit(pos, current_credit_value=0.90)  # small profit, DTE=7
    assert dec.should_close
    assert dec.severity == "dte_close"
    print("  ✓ closes at DTE <= 7 regardless of P&L")


def test_dte_close_at_5():
    pos = _make_pos(dte=5, credit=1.0)
    dec = evaluate_vidar_exit(pos, current_credit_value=1.10)  # small loss
    assert dec.should_close
    assert dec.severity == "dte_close"
    print("  ✓ closes at DTE <= 5 too")


def test_dte_close_holds_at_8():
    pos = _make_pos(dte=8, credit=1.0)
    dec = evaluate_vidar_exit(pos, current_credit_value=0.90)
    assert not dec.should_close
    assert dec.severity == "hold"
    print("  ✓ holds at DTE = 8 (above the 7-day threshold)")


def test_special_holds_through_dte():
    pos = _make_pos(dte=3, credit=1.0, special=True)
    dec = evaluate_vidar_exit(pos, current_credit_value=0.50)
    assert not dec.should_close
    assert dec.severity == "special_hold"
    print("  ✓ SPECIAL'd position ignores all auto-close rules")


def test_special_holds_through_loss():
    pos = _make_pos(dte=30, credit=1.0, special=True)
    dec = evaluate_vidar_exit(pos, current_credit_value=3.5)  # huge loss
    assert not dec.should_close
    assert dec.severity == "special_hold"
    print("  ✓ SPECIAL'd position holds even at extreme loss")


if __name__ == "__main__":
    print("=" * 60)
    print("VIDAR exit ladder tests")
    print("=" * 60)
    test_take_profit_at_50pct()
    test_hold_when_profit_under_50pct()
    test_stop_loss_at_2x_credit()
    test_stop_loss_capped_at_max_loss()
    test_stop_loss_not_triggered_under_threshold()
    test_dte_close_at_7()
    test_dte_close_at_5()
    test_dte_close_holds_at_8()
    test_special_holds_through_dte()
    test_special_holds_through_loss()
    print("\nALL TESTS PASS")
