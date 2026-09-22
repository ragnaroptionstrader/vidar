"""vidar_auto.py — VIDAR orchestrator (phase_pre_build / phase_open / phase_exit_review).

Usage:
    python3 vidar_auto.py pre_build
    python3 vidar_auto.py open
    python3 vidar_auto.py exit_review
    python3 vidar_auto.py pre_build --dry-run

Phases mirror the ragnar/dez structure so cron wiring is uniform.

Cron schedule (paper-only on first deploy, NZST = system TZ on this dev box):
    0 1 * * 1-5  vidar_auto.py pre_build    # 09:00 ET (before RTH open)
    30 1 * * 1-5 vidar_auto.py open         # 09:30 ET (RTH open)
    */20 1-8 * * 1-5 vidar_auto.py exit_review  # every 20m during RTH

Audit tag: vidar.idx (or vidar.special when SPECIAL)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

# Repo paths
VIDAR_HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, "/home/freya/RAGNAR/verticals-bot")
sys.path.insert(0, str(VIDAR_HOME))

from dotenv import load_dotenv
for p in (Path("/home/freya/.env"),
          Path("/home/freya/RAGNAR/verticals-bot/.env"),
          VIDAR_HOME / ".env"):
    if p.exists():
        load_dotenv(p)

# VIDAR packages
from cli.scan import scan_vidar
from vidar_pkg.sizing import size_spy_ic
from vidar_pkg.exits import VidarPosition, evaluate_vidar_exit

# Shared audit log location (same file as DEZ/RAGNAR/FREYA/GUNNAR)
AUDIT_LOG = Path("/home/freya/RAGNAR/verticals_bot_audit.jsonl")
SPECS_DIR = VIDAR_HOME / "ragnar_scripts" / "ragnar_specs"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    print(f"  [{ts}] {msg}")


def _audit(event: dict) -> None:
    """Append a JSONL line to the shared audit log."""
    event.setdefault("ts", datetime.now(timezone.utc).isoformat())
    event.setdefault("strategy", "vidar")
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(event) + "\n")


def phase_pre_build(dry_run: bool = False) -> int:
    """Build today's VIDAR candidate spec. Writes JSON to ragnar_specs/."""
    _log("=== VIDAR pre_build ===")
    spec = scan_vidar(underlying="SPY", dry_run=dry_run)

    today = date.today().strftime("%Y%m%d")
    spec_path = SPECS_DIR / f"vidar_{today}.json"
    if not dry_run:
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec, indent=2))
        _log(f"  wrote spec → {spec_path}")

    if spec.get("picked"):
        p = spec["picked"]
        _log(
            f"  PICKED: SPY {p['expiry']} ({p['dte']} DTE) "
            f"{p['short_put_strike']}P/{p['short_call_strike']}C "
            f"credit ${p['net_credit_total']:.2f} / max risk ${p['max_loss_total']:.2f}"
        )
        _audit({
            "stage": "vidar_pre_build",
            "underlying": "SPY",
            "expiry": p["expiry"],
            "dte": p["dte"],
            "spot": p["spot"],
            "short_put_strike": p["short_put_strike"],
            "short_call_strike": p["short_call_strike"],
            "long_put_strike": p["long_put_strike"],
            "long_call_strike": p["long_call_strike"],
            "net_credit": p["net_credit_total"],
            "max_loss": p["max_loss_total"],
            "max_profit": p["max_profit_total"],
            "setup_reason": "short_premium_idx_vrp",
            "rationale": p["rationale"],
            "tag": "vidar.idx",
            "scope": "vidar",
        })
    else:
        reasons = spec.get("skip_reasons") or ["no_candidate"]
        _log(f"  no pick: {reasons}")
        _audit({
            "stage": "vidar_pre_build",
            "underlying": "SPY",
            "skip_reasons": reasons,
            "tag": "vidar.idx",
            "scope": "vidar",
            "setup_reason": "short_premium_idx_vrp",
        })
    return 0


def phase_open(dry_run: bool = False) -> int:
    """Read today's spec and place the iron condor via place_combo_iron_condor.

    Safety guards (in order):
      1. Spec must exist and have a 'picked' candidate
      2. Sizing must pass caps (max_loss <= $500, qty <= 1)
      3. No existing VIDAR position open
      4. Broker must be reachable
    """
    _log("=== VIDAR open ===")
    today = date.today().strftime("%Y%m%d")
    spec_path = SPECS_DIR / f"vidar_{today}.json"
    if not spec_path.exists():
        _log(f"  ! no spec at {spec_path} — skipping (run pre_build first)")
        return 0

    spec = json.loads(spec_path.read_text())
    p = spec.get("picked")
    if not p:
        _log(f"  ! no candidate in spec (skip_reasons={spec.get('skip_reasons')})")
        return 0

    # Sizing check
    plan = size_spy_ic(
        underlying=p["underlying"],
        short_put_strike=p["short_put_strike"], long_put_strike=p["long_put_strike"],
        short_call_strike=p["short_call_strike"], long_call_strike=p["long_call_strike"],
        net_credit=p["net_credit_per_share"], quantity=1,
    )
    ok, reason = plan.passes_caps()
    if not ok:
        _log(f"  ! sizing cap failed: {reason}")
        _audit({
            "stage": "vidar_open_refused",
            "underlying": p["underlying"],
            "skip_reason": reason,
            "tag": "vidar.idx",
            "scope": "vidar",
        })
        return 0

    if dry_run:
        _log(f"  DRY RUN: would place SPY IC {plan}")
        return 0

    # Place via Tiger broker
    from verticals_bot.broker.tiger_client import TigerConfig, TigerBroker
    account_type = os.environ.get("TIGER_ACCOUNT_TYPE", "paper")
    account_id = os.environ.get("TIGER_ACCOUNT_ID", "21224823943487560")
    private_key_path = os.environ.get(
        "TIGER_PRIVATE_KEY_PATH", "/home/freya/RAGNAR/tiger_openapi_demo.properties"
    )
    b = TigerBroker(TigerConfig(
        account_type=account_type,
        tiger_id=os.environ.get("TIGER_TIGER_ID", "20160454"),
        private_key_path=private_key_path,
        account_id=account_id,
    ))

    # Check for existing VIDAR position (cap: max_open_positions=1)
    existing = [pos for pos in (b._trade_client.get_positions(sec_type='OPT') or [])
                if getattr(pos, 'contract', None) and
                getattr(pos.contract, 'symbol', None) == "SPY"]
    if existing:
        _log(f"  ! {len(existing)} existing SPY position(s) — VIDAR cap is 1, skipping open")
        _audit({
            "stage": "vidar_open_refused",
            "underlying": "SPY",
            "skip_reason": f"existing_positions={len(existing)}>1",
            "tag": "vidar.idx",
            "scope": "vidar",
        })
        return 0

    # Apply mid × 1.02 to all 4 legs (mirrors place_vertical.py safety)
    legs = p["mids"]
    short_put_limit = round(legs["short_put"] * 1.02, 2)
    long_put_limit = round(legs["long_put"] * 1.02, 2)
    short_call_limit = round(legs["short_call"] * 1.02, 2)
    long_call_limit = round(legs["long_call"] * 1.02, 2)

    _log(
        f"  placing IC: SPY {p['expiry']} "
        f"short {p['short_put_strike']}P / {p['short_call_strike']}C "
        f"@ {short_put_limit}/{short_call_limit}; "
        f"long {p['long_put_strike']}P / {p['long_call_strike']}C "
        f"@ {long_put_limit}/{long_call_limit}"
    )

    try:
        result = b.place_combo_iron_condor(
            underlying="SPY",
            expiry=p["expiry"],
            short_call_strike=p["short_call_strike"],
            long_call_strike=p["long_call_strike"],
            short_put_strike=p["short_put_strike"],
            long_put_strike=p["long_put_strike"],
            quantity=1,
            short_call_limit=short_call_limit,
            long_call_limit=long_call_limit,
            short_put_limit=short_put_limit,
            long_put_limit=long_put_limit,
        )
        _log(f"  ✓ placed: order_id={getattr(result, 'order_id', '?')} status={getattr(result, 'status', '?')}")
        _audit({
            "stage": "vidar_open",
            "underlying": "SPY",
            "expiry": p["expiry"],
            "dte": p["dte"],
            "short_put_strike": p["short_put_strike"],
            "short_call_strike": p["short_call_strike"],
            "long_put_strike": p["long_put_strike"],
            "long_call_strike": p["long_call_strike"],
            "net_credit": p["net_credit_total"],
            "max_loss": p["max_loss_total"],
            "order_id": getattr(result, 'order_id', None),
            "status": str(getattr(result, 'status', None)),
            "setup_reason": "short_premium_idx_vrp",
            "tag": "vidar.idx",
            "scope": "vidar",
        })
    except Exception as exc:
        _log(f"  ! place_combo_iron_condor failed: {exc}")
        _audit({
            "stage": "vidar_open_failed",
            "underlying": "SPY",
            "error": str(exc),
            "tag": "vidar.idx",
            "scope": "vidar",
        })
        return 1
    return 0


def phase_exit_review(dry_run: bool = False) -> int:
    """For each VIDAR position, evaluate exit criteria. Auto-close if triggered."""
    _log("=== VIDAR exit_review ===")

    from verticals_bot.broker.tiger_client import TigerConfig, TigerBroker
    account_type = os.environ.get("TIGER_ACCOUNT_TYPE", "paper")
    account_id = os.environ.get("TIGER_ACCOUNT_ID", "21224823943487560")
    private_key_path = os.environ.get(
        "TIGER_PRIVATE_KEY_PATH", "/home/freya/RAGNAR/tiger_openapi_demo.properties"
    )
    b = TigerBroker(TigerConfig(
        account_type=account_type,
        tiger_id=os.environ.get("TIGER_TIGER_ID", "20160454"),
        private_key_path=private_key_path,
        account_id=account_id,
    ))

    # Look for VIDAR positions — OPT only, SPY, with the vidar.idx tag in audit
    opt_positions = b._trade_client.get_positions(sec_type='OPT') or []
    spy_legs = []
    for pos in opt_positions:
        c = pos.contract
        if not (hasattr(c, 'symbol') and c.symbol == "SPY"):
            continue
        spy_legs.append({
            "strike": c.strike,
            "right": c.right[0] if hasattr(c, 'right') and c.right else "?",
            "expiry": c.expiry if hasattr(c, 'expiry') else "?",
            "quantity": pos.quantity,
            "market_price": getattr(pos, 'market_price', None) or getattr(pos, 'latest_price', 0),
        })
    if not spy_legs:
        _log("  no VIDAR/SPY positions open — nothing to do")
        return 0

    # Group by expiry — VIDAR positions share an expiry across 4 legs
    expiries = {leg['expiry'] for leg in spy_legs}
    for expiry in expiries:
        legs_for_exp = [leg for leg in spy_legs if leg['expiry'] == expiry]
        if len(legs_for_exp) != 4:
            _log(f"  skip expiry {expiry}: {len(legs_for_exp)} legs (expect 4)")
            continue

        # Pull the original open from audit log to get net_credit
        # Look back for the most recent vidar_open for SPY + this expiry
        net_credit = 1.0  # fallback if not found in audit
        if AUDIT_LOG.exists():
            for line in reversed(AUDIT_LOG.read_text().splitlines()[-200:]):
                try:
                    ev = json.loads(line)
                    if (ev.get("stage") == "vidar_open" and
                        ev.get("underlying") == "SPY" and
                        ev.get("expiry") == expiry):
                        net_credit = ev.get("net_credit", 1.0) / 100.0  # audit stores $
                        break
                except Exception:
                    pass

        # Determine short vs long: sort puts ASC, calls DESC; shorts are inner
        puts = sorted([l for l in legs_for_exp if l['right'] == 'P'], key=lambda x: x['strike'])
        calls = sorted([l for l in legs_for_exp if l['right'] == 'C'], key=lambda x: x['strike'])
        if len(puts) != 2 or len(calls) != 2:
            _log(f"  skip expiry {expiry}: leg structure not 2P+2C")
            continue
        # Inner strikes (short) are higher put + lower call
        short_put = puts[1]
        long_put = puts[0]
        short_call = calls[0]
        long_call = calls[1]

        # Current cost to close = sum of all 4 legs' market prices
        # (multiplied by sign — short pays to buy back, long sells to close)
        # For net cost: shorts add, longs subtract
        try:
            current_credit_value = (
                float(short_put['market_price'] or 0) +
                float(short_call['market_price'] or 0) -
                float(long_put['market_price'] or 0) -
                float(long_call['market_price'] or 0)
            )
        except Exception:
            current_credit_value = net_credit  # fallback

        # Build position for exit eval
        try:
            expiry_date = date.fromisoformat(expiry)
        except Exception:
            continue

        pos = VidarPosition(
            underlying="SPY", expiry=expiry,
            short_put_strike=float(short_put['strike']),
            long_put_strike=float(long_put['strike']),
            short_call_strike=float(short_call['strike']),
            long_call_strike=float(long_call['strike']),
            net_credit=net_credit,
            quantity=1,
            open_date=(date.today().isoformat()),
        )

        dec = evaluate_vidar_exit(pos, current_credit_value=current_credit_value)
        _log(
            f"  SPY {expiry}: P&L ${dec.current_pnl:+.2f}, "
            f"DTE {pos.dte}, severity={dec.severity} → "
            f"{'CLOSE' if dec.should_close else 'HOLD'}"
        )
        _audit({
            "stage": "vidar_exit_review",
            "underlying": "SPY",
            "expiry": expiry,
            "current_credit_value": current_credit_value,
            "pnl": dec.current_pnl,
            "dte": pos.dte,
            "severity": dec.severity,
            "decision": "close" if dec.should_close else "hold",
            "reason": dec.reason,
            "tag": "vidar.idx",
            "scope": "vidar",
            "setup_reason": "short_premium_idx_vrp",
        })
        if dec.should_close and not dry_run:
            _log(f"  → auto-close logic TBD (will wire once paper-trade validates)")
            # TODO: build close orders via TigerBroker.submit_order
    return 0


def main():
    parser = argparse.ArgumentParser(description="VIDAR orchestrator")
    parser.add_argument("phase", choices=["pre_build", "open", "exit_review", "pre_close", "eod"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.phase == "pre_build":
        return phase_pre_build(dry_run=args.dry_run)
    if args.phase == "open":
        return phase_open(dry_run=args.dry_run)
    if args.phase == "exit_review":
        return phase_exit_review(dry_run=args.dry_run)
    if args.phase in ("pre_close", "eod"):
        _log(f"=== VIDAR {args.phase} (no-op — vertical carries overnight) ===")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
