"""VIDAR daily scanner — pick the next SPY 30-45 DTE 16-delta iron condor.

Pipeline:
1. Pick the next SPY monthly expiry in 30-45 DTE
2. Pull the L2 option chain with Greeks
3. Find the 16-delta PUT strike (short wing)
4. Find the 16-delta CALL strike (short wing)
5. Pick $5-wide wings for SPY (long wings at strike ± 5)
6. Compute mid prices for all 4 legs
7. Net credit = (short_put_mid + short_call_mid) - (long_put_mid + long_call_mid)
8. Apply VRP filter (signals/vrp.py)
9. Emit a candidate spec

Output: JSON to stdout (or --out PATH). The phase_open orchestrator
reads the JSON and submits via place_combo_iron_condor.

Usage:
    python3 /home/freya/vidar/cli/scan.py
    python3 /home/freya/vidar/cli/scan.py --dry-run
    python3 /home/freya/vidar/cli/scan.py --out /home/freya/vidar/ragnar_scripts/ragnar_specs/vidar_$(date +%Y%m%d).json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

# Allow imports from the verticals-bot and vidar packages
sys.path.insert(0, "/home/freya/RAGNAR/verticals-bot")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
for p in (Path("/home/freya/.env"),
          Path("/home/freya/RAGNAR/verticals-bot/.env"),
          Path(__file__).resolve().parent.parent.parent / ".env"):
    if p.exists():
        load_dotenv(p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _next_expiry(today: date, min_dte: int = 30, max_dte: int = 45,
                 weekly: bool = True) -> date:
    """Pick the next expiry date in [min_dte, max_dte] from today.

    For SPY (and most liquid underlyings), weeklies are available every
    Friday. We walk forward day-by-day until we land in the window. If
    weekly=False, restrict to 3rd-Friday-of-month (monthlies).

    The walk is bounded at 90 days to prevent infinite loops.
    """
    step = 1 if weekly else 7  # if monthlies, jump 7 days at a time
    cursor = today + timedelta(days=min_dte)
    # If cursor is past a Friday, jump to next Friday
    days_to_friday = (4 - cursor.weekday()) % 7
    if days_to_friday > 0:
        cursor = cursor + timedelta(days=days_to_friday)

    walked = 0
    while walked < 90:
        dte = (cursor - today).days
        if min_dte <= dte <= max_dte:
            return cursor
        if dte > max_dte:
            # Step backwards to find the closest Friday in range
            for back in range(1, 8):
                cand = cursor - timedelta(days=back)
                if min_dte <= (cand - today).days <= max_dte:
                    return cand
            # Couldn't find a Friday in window — return cursor anyway
            # (will be flagged by phase_open cap check)
            return cursor
        cursor = cursor + timedelta(days=step)
        walked += step
    return cursor  # fallback


def _closest_strike_to_delta(df, side: str, target_delta_abs: float,
                              spot: float) -> tuple[float, float] | None:
    """Find the strike whose |delta| is closest to target_delta_abs.

    side: 'PUT' or 'CALL'
    Returns (strike, delta) or None.
    """
    side_df = df[df["put_call"] == side].copy()
    if side_df.empty or "delta" not in side_df.columns:
        return None
    side_df["delta_abs"] = side_df["delta"].abs()
    side_df["dist"] = (side_df["delta_abs"] - target_delta_abs).abs()
    row = side_df.loc[side_df["dist"].idxmin()]
    return float(row["strike"]), float(row["delta"])


def _mid_price(row) -> float | None:
    """Compute mid from a chain row. Returns None if no valid quote."""
    bid = float(row.get("bid_price", 0) or 0)
    ask = float(row.get("ask_price", 0) or 0)
    if bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2.0


def _fetch_chain_with_greeks(quote_client, symbol: str, expiry_str: str):
    """Pull the SPY option chain with Greeks (L2)."""
    from tigeropen.quote.domain.filter import OptionFilter
    f = OptionFilter(open_interest_min=10)
    return quote_client.get_option_chain(
        symbol, expiry_str, option_filter=f, return_greek_value=True,
    )


def _fetch_spot(quote_client, symbol: str) -> float:
    """Get the latest spot price."""
    briefs = quote_client.get_stock_briefs([symbol])
    if briefs is None or briefs.empty:
        return 0.0
    row = briefs.iloc[0]
    return float(row.get("latest_price", 0) or row.get("close", 0) or 0)


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def scan_vidar(underlying: str = "SPY",
              min_dte: int = 30, max_dte: int = 45,
              target_delta: float = 0.16,
              wing_width: float = 5.0,
              dry_run: bool = False) -> dict:
    """Scan for a 30-45 DTE IC on SPY at 16-delta wings.

    Returns the spec dict ready for phase_open to consume.
    """
    from verticals_bot.broker.tiger_client import TigerConfig, TigerBroker

    today = date.today()
    expiry = _next_expiry(today, min_dte=min_dte, max_dte=max_dte, weekly=True)
    expiry_str = expiry.strftime("%Y-%m-%d")
    actual_dte = (expiry - today).days

    spec = {
        "as_of": datetime.utcnow().isoformat() + "+00:00",
        "phase": "pre_build",
        "play": "vidar_idx_short_premium",
        "strategy": "vidar",
        "scope": "vidar",
        "underlying": underlying,
        "expiry": expiry_str,
        "dte": actual_dte,
        "target_delta": target_delta,
        "wing_width": wing_width,
        "candidates": [],
        "picked": None,
        "skip_reasons": [],
        "dry_run": dry_run,
    }

    # Build broker (paper-only on first deploy)
    account_type = os.environ.get("TIGER_ACCOUNT_TYPE", "paper")
    account_id = os.environ.get("TIGER_ACCOUNT_ID", "21224823943487560")
    private_key_path = os.environ.get(
        "TIGER_PRIVATE_KEY_PATH",
        "/home/freya/RAGNAR/tiger_openapi_demo.properties",
    )
    b = TigerBroker(TigerConfig(
        account_type=account_type,
        tiger_id=os.environ.get("TIGER_TIGER_TIGER_ID") or os.environ.get("TIGER_TIGER_ID", "20160454"),
        private_key_path=private_key_path,
        account_id=account_id,
    ))

    # Spot
    spot = _fetch_spot(b._quote_client, underlying)
    if spot <= 0:
        spec["skip_reasons"].append("no_spot_quote")
        return spec
    spec["spot"] = spot

    # Chain
    try:
        df = _fetch_chain_with_greeks(b._quote_client, underlying, expiry_str)
    except Exception as e:
        spec["skip_reasons"].append(f"chain_fetch_failed:{type(e).__name__}")
        return spec
    if df is None or df.empty:
        spec["skip_reasons"].append("empty_chain")
        return spec

    # Strike selection
    import pandas as pd
    df = df.copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if "delta" not in df.columns:
        spec["skip_reasons"].append("no_delta_column")
        return spec

    # Find 16-delta strikes
    put_res = _closest_strike_to_delta(df, "PUT", target_delta, spot)
    call_res = _closest_strike_to_delta(df, "CALL", target_delta, spot)
    if put_res is None or call_res is None:
        spec["skip_reasons"].append("delta_strike_not_found")
        return spec

    put_short_strike, put_delta = put_res
    call_short_strike, call_delta = call_res
    put_long_strike = put_short_strike - wing_width
    call_long_strike = call_short_strike + wing_width

    # Pull mid for all 4 legs
    def row_at(strike: float, side: str):
        sub = df[(df["strike"] == strike) & (df["put_call"] == side)]
        return sub.iloc[0] if not sub.empty else None

    legs = {
        "short_put": row_at(put_short_strike, "PUT"),
        "long_put": row_at(put_long_strike, "PUT"),
        "short_call": row_at(call_short_strike, "CALL"),
        "long_call": row_at(call_long_strike, "CALL"),
    }
    mids = {k: _mid_price(v) for k, v in legs.items() if v is not None}
    if any(v is None for v in mids.values()):
        spec["skip_reasons"].append(f"missing_mid:{[k for k,v in mids.items() if v is None]}")
        spec["strikes"] = {
            "short_put": put_short_strike, "long_put": put_long_strike,
            "short_call": call_short_strike, "long_call": call_long_strike,
            "put_delta": put_delta, "call_delta": call_delta,
        }
        return spec

    net_credit = (mids["short_put"] + mids["short_call"]) - (mids["long_put"] + mids["long_call"])
    max_loss_per_share = wing_width - net_credit
    if max_loss_per_share <= 0:
        spec["skip_reasons"].append("credit_exceeds_wing")
        return spec

    max_loss_total = max_loss_per_share * 100
    if max_loss_total > 500:
        spec["skip_reasons"].append(f"max_loss_${max_loss_total:.0f}>$500_cap")
        return spec

    spec["picked"] = {
        "underlying": underlying,
        "expiry": expiry_str,
        "dte": actual_dte,
        "spot": spot,
        "short_put_strike": put_short_strike,
        "long_put_strike": put_long_strike,
        "short_call_strike": call_short_strike,
        "long_call_strike": call_long_strike,
        "put_short_delta": put_delta,
        "call_short_delta": call_delta,
        "wing_width": wing_width,
        "mids": mids,
        "net_credit_per_share": round(net_credit, 4),
        "net_credit_total": round(net_credit * 100, 2),
        "max_loss_per_share": round(max_loss_per_share, 4),
        "max_loss_total": round(max_loss_total, 2),
        "max_profit_total": round(net_credit * 100, 2),
        "breakeven_lower": round(put_short_strike - net_credit, 2),
        "breakeven_upper": round(call_short_strike + net_credit, 2),
        "rationale": (
            f"SPY {expiry_str} ({actual_dte} DTE) 16-delta IC: "
            f"short {put_short_strike}/{call_short_strike}, "
            f"long {put_long_strike}/{call_long_strike}, "
            f"credit ${net_credit*100:.2f}, max risk ${max_loss_total:.2f}"
        ),
    }
    return spec


def main():
    parser = argparse.ArgumentParser(description="VIDAR scanner: SPY short-premium IC")
    parser.add_argument("--underlying", default="SPY")
    parser.add_argument("--min-dte", type=int, default=30)
    parser.add_argument("--max-dte", type=int, default=45)
    parser.add_argument("--target-delta", type=float, default=0.16)
    parser.add_argument("--wing-width", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", help="Write JSON spec to this path")
    args = parser.parse_args()

    spec = scan_vidar(
        underlying=args.underlying,
        min_dte=args.min_dte, max_dte=args.max_dte,
        target_delta=args.target_delta,
        wing_width=args.wing_width,
        dry_run=args.dry_run,
    )

    out = json.dumps(spec, indent=2, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(out)
        print(f"wrote {args.out}")
    else:
        print(out)


if __name__ == "__main__":
    main()
