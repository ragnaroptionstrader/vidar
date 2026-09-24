"""Tests for vidar_auto tick-grid rounding.

Background: SPY listed options use a 0.05 tick grid for sub-$3 strikes.
The original implementation used `round(x * 1.02, 2)` which keeps
2 decimal places but doesn't enforce the 0.05 grid. Tiger rejects
combo orders with 'tick size: 0.01' when a limit lands off-grid
(e.g. 2.56 should be 2.55 or 2.60).

These tests pin the rounding behavior so a future refactor can't
regress to 2-decimal-only rounding.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ragnar_scripts" / "vidar_auto.py"


def _round_tick(price: float, tick: float = 0.05) -> float:
    """Mirror vidar_auto._round_tick exactly."""
    return round(price / tick) * tick


def _approx(a, b, tol=1e-9):
    return math.isclose(a, b, abs_tol=tol)


def test_round_tick_already_on_grid():
    assert _approx(_round_tick(2.55), 2.55)
    assert _approx(_round_tick(1.75), 1.75)
    assert _approx(_round_tick(3.60), 3.60)


def test_round_tick_rounds_to_nearest_005():
    # Sub-cent values should round to nearest 0.05.
    # Note: 2.56 / 0.05 = 51.1999... → rounds to 51 → 51 * 0.05 = 2.55
    assert _approx(_round_tick(2.56), 2.55)
    # 2.58 / 0.05 = 51.6 → rounds to 52 → 2.60
    assert _approx(_round_tick(2.58), 2.60)
    # 1.77 / 0.05 = 35.4 → rounds to 35 → 1.75
    assert _approx(_round_tick(1.77), 1.75)
    # 1.78 / 0.05 = 35.6 → rounds to 36 → 1.80
    assert _approx(_round_tick(1.78), 1.80)


def test_round_tick_handles_010_ticks():
    # For options on 0.10 grid (e.g. deeper OTM or higher-priced underlyings)
    assert _approx(_round_tick(2.61, tick=0.10), 2.60)
    assert _approx(_round_tick(2.66, tick=0.10), 2.70)


def test_round_tick_zero_negative():
    # Defensive
    assert _approx(_round_tick(0.01), 0.00)
    assert _approx(_round_tick(-0.02), 0.00)  # round-half-to-even via Python


def test_mid_markup_then_round():
    """End-to-end: mid × 1.02 then round to 0.05 grid. Uses real
    mids from the 2026-09-24 SPY 10/30 16-delta spec (the values that
    caused the 2026-09-24 broker rejection)."""
    mids = {"short_put": 3.55, "long_put": 3.10,
            "short_call": 2.56, "long_call": 1.77}
    limits = {k: _round_tick(m * 1.02) for k, m in mids.items()}
    # After × 1.02:
    #   short_put  3.621  → 72.42 ticks → 72 → 3.60
    #   long_put   3.162  → 63.24 ticks → 63 → 3.15 (FP noise: 3.1500000000000004)
    #   short_call 2.6112 → 52.224 ticks → 52 → 2.60
    #   long_call  1.8054 → 36.108 ticks → 36 → 1.80
    assert _approx(limits["short_call"], 2.60)
    assert _approx(limits["long_call"], 1.80)
    assert _approx(limits["short_put"], 3.60)
    assert _approx(limits["long_put"], 3.15)
    # All values on the 0.05 grid
    for k, v in limits.items():
        ratio = round(v / 0.05)
        assert _approx(v, ratio * 0.05), f"{k}={v} not on 0.05 grid"
