"""VIDAR sizing — fixed-fractional 1-contract cap.

The $750 envelope dictates:
  - 1 SPY 30-45 DTE IC at $5-wide wings = ~$350 max loss
  - Wing width × 100 = max risk per contract (filled at expiry)
  - Net credit collected ~$1.50-2.50 per contract
  - Max loss = wing_width - credit (per share) × 100

Sizing rules:
  - max_contracts_per_trade: 1 (cap by envelope, not by VaR)
  - max_loss_per_trade_usd: 500.0 (margin of safety above $350 max)
  - max_daily_loss_usd: 500.0 (one new position per day max)
  - max_open_positions: 1 (no scaling until envelope grows)

The sizing module refuses to compute a plan that exceeds any cap,
returning None. The caller (phase_open) writes an audit log entry
explaining the refusal.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VidarSizing:
    """One position sizing plan."""

    underlying: str
    quantity: int             # always 1 for VIDAR.idx
    credit_per_share: float   # expected credit received
    wing_width: float         # strike spread between short and long
    max_loss_per_share: float # wing_width - credit
    max_profit_total: float   # credit × 100 × qty
    max_loss_total: float     # max_loss_per_share × 100 × qty
    breakeven_lower: float    # short_put_strike - credit
    breakeven_upper: float    # short_call_strike + credit

    def passes_caps(
        self,
        max_contracts_per_trade: int = 1,
        max_loss_per_trade_usd: float = 500.0,
    ) -> tuple[bool, str]:
        if self.quantity > max_contracts_per_trade:
            return False, f"qty {self.quantity} > cap {max_contracts_per_trade}"
        if self.max_loss_total > max_loss_per_trade_usd:
            return False, (
                f"max_loss ${self.max_loss_total:.0f} > cap ${max_loss_per_trade_usd:.0f}"
            )
        return True, ""


def size_spy_ic(
    underlying: str,
    short_put_strike: float,
    long_put_strike: float,
    short_call_strike: float,
    long_call_strike: float,
    net_credit: float,
    quantity: int = 1,
) -> VidarSizing:
    """Compute the sizing plan for a SPY iron condor.

    All 4 strikes must be on the same expiry.
    """
    if not (short_put_strike > long_put_strike):
        raise ValueError(
            f"short_put_strike {short_put_strike} must be > long_put_strike {long_put_strike}"
        )
    if not (long_call_strike > short_call_strike):
        raise ValueError(
            f"long_call_strike {long_call_strike} must be > short_call_strike {short_call_strike}"
        )
    if net_credit <= 0:
        raise ValueError(f"net_credit {net_credit} must be > 0")

    put_wing_width = short_put_strike - long_put_strike
    call_wing_width = long_call_strike - short_call_strike
    if abs(put_wing_width - call_wing_width) > 0.01:
        raise ValueError(
            f"wing widths unequal: put {put_wing_width} vs call {call_wing_width}"
        )

    wing_width = put_wing_width
    max_loss_per_share = wing_width - net_credit
    if max_loss_per_share <= 0:
        raise ValueError("credit exceeds wing width — credit bigger than risk")

    return VidarSizing(
        underlying=underlying,
        quantity=quantity,
        credit_per_share=net_credit,
        wing_width=wing_width,
        max_loss_per_share=max_loss_per_share,
        max_profit_total=net_credit * 100 * quantity,
        max_loss_total=max_loss_per_share * 100 * quantity,
        breakeven_lower=short_put_strike - net_credit,
        breakeven_upper=short_call_strike + net_credit,
    )
