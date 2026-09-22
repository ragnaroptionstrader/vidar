"""VIDAR exit ladder — 50% profit / 2x credit stop / DTE-7.

The VIDAR exit policy mirrors the short-premium literature (Coval &
Shumway 2005, BXM 2002). Tight profit-take captures theta before
the position reverses; the 2x credit stop is the canonical
defined-risk cap.

Per-position exit_pnl captures realized gain/loss when the cron
fires phase_exit_review.

SPECIAL semantics:
  - Same as DEZ — manual override suppresses auto-close for one position.
  - Operator marks with `mark_special_position.py --tag vidar.special`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class VidarPosition:
    """One open VIDAR position for exit evaluation."""

    underlying: str
    expiry: str              # YYYY-MM-DD
    short_put_strike: float
    long_put_strike: float
    short_call_strike: float
    long_call_strike: float
    net_credit: float        # credit received at entry, per share
    quantity: int            # contracts (each = 100 shares)
    open_date: str           # YYYY-MM-DD
    tag: str = "vidar.idx"    # strategy tag for ODIN
    is_special: bool = False # if True, skip auto-close (operator override)

    @property
    def max_loss_per_share(self) -> float:
        wing = self.short_put_strike - self.long_put_strike
        return wing - self.net_credit

    @property
    def max_loss_total(self) -> float:
        return self.max_loss_per_share * 100 * self.quantity

    @property
    def max_profit_total(self) -> float:
        return self.net_credit * 100 * self.quantity

    @property
    def dte(self) -> int:
        try:
            exp_date = date.fromisoformat(self.expiry)
        except (ValueError, TypeError):
            return 0
        return (exp_date - date.today()).days


@dataclass
class VidarExitDecision:
    should_close: bool
    reason: str
    severity: str   # 'take_profit', 'stop_loss', 'dte_close', 'hold', 'special_hold'
    current_pnl: float
    pnl_pct: float


def evaluate_vidar_exit(
    pos: VidarPosition,
    current_credit_value: float,  # current cost to BUY BACK the IC (per share)
    take_profit_pct: float = 50.0,
    stop_loss_credit_mult: float = 2.0,
    dte_min: int = 7,
) -> VidarExitDecision:
    """Decide whether to close a VIDAR iron condor.

    Args:
        pos: the open position
        current_credit_value: per-share debit to close the position now.
            Higher = worse for us = more loss. At entry this is ~net_credit.
            As the position profits, current_credit_value drops.
        take_profit_pct: close when realized gain >= X% of max profit (default 50)
        stop_loss_credit_mult: close when realized loss >= X × credit received
            (default 2.0 = "2x credit stop" — the canonical short-premium stop).
            Capped at max_loss_per_share (no point triggering beyond wing width).
        dte_min: close when DTE <= N (last-week gamma risk)

    Returns:
        VidarExitDecision

    Note on stop_loss_pct vs stop_loss_credit_mult:
      The previous `stop_loss_pct` (% of max_loss) is broken for short premium:
      max_loss is the absolute cap on loss (at expiry, the position can lose at
      most max_loss), so pct_of_loss can never reach >100%. The 2x-credit stop
      is the canonical short-premium rule from Coval-Shumway / BuyWrite lit.
    """
    if pos.is_special:
        return VidarExitDecision(
            should_close=False, reason="SPECIAL: manual hold",
            severity="special_hold", current_pnl=0.0, pnl_pct=0.0,
        )

    # P&L = (credit_received - cost_to_close) × 100 × qty
    pnl_per_share = pos.net_credit - current_credit_value
    pnl_total = pnl_per_share * 100 * pos.quantity

    # Take profit (pnl_total > 0, % of max profit)
    if pnl_total > 0 and pos.max_profit_total > 0:
        pct_of_profit = (pnl_total / pos.max_profit_total) * 100
        if pct_of_profit >= take_profit_pct:
            return VidarExitDecision(
                should_close=True,
                reason=f"take_profit: {pct_of_profit:.1f}% of max profit (target {take_profit_pct}%)",
                severity="take_profit",
                current_pnl=pnl_total,
                pnl_pct=pct_of_profit,
            )

    # Stop loss (pnl_total < 0; threshold = stop_loss_credit_mult × credit)
    if pnl_total < 0:
        threshold_dollars = stop_loss_credit_mult * pos.net_credit * 100 * pos.quantity
        # Cap at max_loss (position can't lose more than max_loss_total)
        threshold_dollars = min(threshold_dollars, abs(pos.max_loss_total))
        if abs(pnl_total) >= threshold_dollars:
            return VidarExitDecision(
                should_close=True,
                reason=(
                    f"stop_loss: loss ${abs(pnl_total):.2f} >= "
                    f"{stop_loss_credit_mult}x credit (${threshold_dollars:.2f})"
                ),
                severity="stop_loss",
                current_pnl=pnl_total,
                pnl_pct=0.0,
            )

    # DTE close
    dte = pos.dte
    if dte <= dte_min:
        return VidarExitDecision(
            should_close=True,
            reason=f"dte_close: DTE {dte} <= {dte_min} (last-week gamma risk)",
            severity="dte_close",
            current_pnl=pnl_total,
            pnl_pct=0.0,
        )

    return VidarExitDecision(
        should_close=False,
        reason=f"hold: P&L ${pnl_total:+.2f}, DTE {dte}",
        severity="hold",
        current_pnl=pnl_total,
        pnl_pct=0.0,
    )
