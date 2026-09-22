"""VRP scoring for VIDAR — volatility risk premium edge detector.

The VRP is the gap between implied volatility (what options price in)
and realized volatility (what the underlying actually delivers). When
IV > RV by enough margin, selling premium captures the spread.

This module answers: "is now a good time to sell SPY 30-45 DTE premium?"

Three filters layered:

1. **IV > RV** — current ATM IV must be ≥ realized vol (annualized).
   Strictly speaking, VRP is positive most of the time in SPY (Carr
   & Wu 2009). We use this as a sanity check, not a hard gate.

2. **Term structure check** — short-dated IV (≤7 DTE) should NOT be
   spiking above 30-DTE IV (would signal an imminent event). If it
   does, defer the entry.

3. **Earnings/event calendar** — avoid opening 30-45 DTE positions
   that would have earnings or FOMC inside the DTE window. Earnings
   vol crush can whipsaw both wings.

Output: {eligible: bool, vrp_edge_pct, rationale}
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Optional


@dataclass(frozen=True)
class VRPVerdict:
    eligible: bool
    vrp_edge_pct: float       # (IV - RV) / RV as percent (positive = edge)
    iv_annualized: float      # current ATM IV as decimal (0.16 = 16%)
    rv_annualized: float      # realized vol (annualized)
    term_ratio: float         # short_dte_iv / long_dte_iv
    rationale: str
    skip_reason: str = ""


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def annualized_rv_from_log_returns(log_rets: list[float], window: int = 20) -> float:
    """Compute annualized realized vol from a series of log returns.

    Uses the most recent `window` returns. Returns 0 if input is
    too short.
    """
    if len(log_rets) < window:
        return 0.0
    recent = log_rets[-window:]
    mean = sum(recent) / len(recent)
    var = sum((r - mean) ** 2 for r in recent) / (len(recent) - 1)
    return math.sqrt(var) * math.sqrt(252.0)


def has_event_in_dte_window(start: date, dte_days: int,
                              earnings_dates: list[date],
                              fomc_dates: list[date]) -> Optional[str]:
    """Check if any earnings or FOMC dates fall in the DTE window.

    Returns the offending date label as a string, or None if clean.
    """
    end = start + timedelta(days=dte_days)
    for ed in earnings_dates:
        if start <= ed <= end:
            return f"earnings on {ed.isoformat()}"
    for fd in fomc_dates:
        if start <= fd <= end:
            return f"FOMC on {fd.isoformat()}"
    return None


def evaluate_vrp(
    iv_atm: float,
    log_returns: list[float],
    iv_short_dte: float,
    iv_long_dte: float,
    start: date,
    dte_days: int,
    earnings_dates: list[date],
    fomc_dates: list[date],
    min_vrp_edge_pct: float = 5.0,
) -> VRPVerdict:
    """Evaluate whether to enter a short-premium position now.

    Args:
        iv_atm: current ATM IV (annualized, decimal — 0.16 = 16%)
        log_returns: recent daily log returns of the underlying
        iv_short_dte: ATM IV at short DTE (e.g. 7 DTE)
        iv_long_dte: ATM IV at long DTE (e.g. 30 DTE)
        start: today's date
        dte_days: target DTE for the new position (e.g. 35)
        earnings_dates: list of earnings dates for this underlying
        fomc_dates: list of FOMC dates
        min_vrp_edge_pct: minimum IV-RV edge to enter (default 5%)

    Returns:
        VRPVerdict with eligible + reasoning
    """
    rv = annualized_rv_from_log_returns(log_returns, window=20)
    if rv <= 0:
        return VRPVerdict(
            eligible=False, vrp_edge_pct=0.0,
            iv_annualized=iv_atm, rv_annualized=0.0,
            term_ratio=0.0,
            rationale="no realized vol data", skip_reason="no_rv_data",
        )

    vrp_edge_pct = ((iv_atm - rv) / rv) * 100.0
    term_ratio = iv_short_dte / iv_long_dte if iv_long_dte > 0 else 0.0

    # Filter 1: VRP must be positive and meaningful
    if vrp_edge_pct < min_vrp_edge_pct:
        return VRPVerdict(
            eligible=False, vrp_edge_pct=vrp_edge_pct,
            iv_annualized=iv_atm, rv_annualized=rv,
            term_ratio=term_ratio,
            rationale=f"VRP edge {vrp_edge_pct:.1f}% < {min_vrp_edge_pct}% floor",
            skip_reason="vrp_too_thin",
        )

    # Filter 2: term structure — short should NOT exceed long by a lot
    if term_ratio > 1.15:
        return VRPVerdict(
            eligible=False, vrp_edge_pct=vrp_edge_pct,
            iv_annualized=iv_atm, rv_annualized=rv,
            term_ratio=term_ratio,
            rationale=f"short DTE IV {term_ratio:.2f}x long DTE — event-driven spike",
            skip_reason="term_inverted",
        )

    # Filter 3: no earnings/FOMC in the DTE window
    event = has_event_in_dte_window(start, dte_days, earnings_dates, fomc_dates)
    if event:
        return VRPVerdict(
            eligible=False, vrp_edge_pct=vrp_edge_pct,
            iv_annualized=iv_atm, rv_annualized=rv,
            term_ratio=term_ratio,
            rationale=f"deferred — {event} inside {dte_days}d window",
            skip_reason="event_in_window",
        )

    return VRPVerdict(
        eligible=True, vrp_edge_pct=vrp_edge_pct,
        iv_annualized=iv_atm, rv_annualized=rv,
        term_ratio=term_ratio,
        rationale=f"VRP edge {vrp_edge_pct:.1f}% (IV {iv_atm*100:.1f}% vs RV {rv*100:.1f}%), term {term_ratio:.2f}",
    )
