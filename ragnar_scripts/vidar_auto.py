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

# 2026-09-25: cap is VIDAR-only. The previous check counted ALL SPY positions
# (regardless of which strategy owned them), so other strategies' positions
# or testing/manual probes could block VIDAR from opening. Now we walk the
# audit log and count only VIDAR-owned ICs — those opened via
# `stage=vidar_open status=FILLED` that haven't been offset by a close.
# Default cap is 1 (matches the original semantics — VIDAR is short-premium
# and doesn't stack positions). Override via VIDAR_MAX_OPEN_ICS env var.
MAX_VIDAR_ICS = int(os.environ.get("VIDAR_MAX_OPEN_ICS", "1"))


def _count_vidar_open_ics(broker, audit_log_path: Path = AUDIT_LOG) -> tuple[int, list[tuple]]:
    """Count actually-open VIDAR iron condors by cross-referencing audit
    log with current broker positions.

    A vidar_open audit event counts as 'actually open' if at least one
    of its 4 strikes has a current broker position. This:
      - correctly ignores cron orders that were never filled (audit
        status=PENDING is stale; cron doesn't poll fill status)
      - correctly counts cron orders that DID fill but had some legs
        closed (orphan leg still has exposure)
      - correctly excludes ICs that were fully closed (no broker legs)

    Returns (count, [(sym, expiry, [strikes])...]) for the active set.

    The previous version counted vidar_open events alone, which over-
    counted by including orders that the broker rejected/expired before
    filling but were never re-marked in the audit log. Caught 2026-09-25.
    """
    from collections import defaultdict

    # 1. Get the set of (sym, expiry, strike) tuples that have current
    #    broker positions in OPT.
    broker_legs: set[tuple] = set()
    positions = broker._trade_client.get_positions(sec_type="OPT") or []
    for pos in positions:
        c = pos.contract
        if not c:
            continue
        sym = getattr(c, "symbol", "")
        expiry = getattr(c, "expiry", "")
        strike = float(getattr(c, "strike", 0) or 0)
        broker_legs.add((sym, expiry, strike))

    # 2. Walk audit log for vidar_open events with non-REJECTED status.
    leg_counts: dict[tuple, int] = defaultdict(int)
    ics: list[dict] = []

    if not audit_log_path.exists():
        return 0, []

    with audit_log_path.open() as f:
        for line in f:
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if ev.get("strategy") != "vidar":
                continue

            stage = ev.get("stage", "")
            status = ev.get("status", "")
            sym = ev.get("underlying", "")
            expiry = ev.get("expiry", "")

            if stage == "vidar_open" and status != "OrderStatus.REJECTED":
                strikes = [
                    ev.get("short_put_strike"),
                    ev.get("short_call_strike"),
                    ev.get("long_put_strike"),
                    ev.get("long_call_strike"),
                ]
                if all(s for s in strikes):
                    ics.append({"sym": sym, "expiry": expiry, "strikes": strikes})
                    for s in strikes:
                        leg_counts[(sym, expiry, s)] = (
                            leg_counts.get((sym, expiry, s), 0) + 1
                        )
            elif stage in ("vidar_close", "broker_expired",
                          "stranded_leg_auto_close", "manual_close"):
                close_strike = ev.get("strike", 0)
                if close_strike:
                    leg_counts[(sym, expiry, close_strike)] = (
                        leg_counts.get((sym, expiry, close_strike), 0) - 1
                    )

    # 3. An IC is "actually open" if ANY of its strikes has BOTH:
    #    - positive audit count (not fully closed in audit)
    #    - a current broker position (still held at the broker)
    active_ics = []
    for ic in ics:
        any_open = any(
            leg_counts.get((ic["sym"], ic["expiry"], s), 0) > 0
            and (ic["sym"], ic["expiry"], s) in broker_legs
            for s in ic["strikes"]
        )
        if any_open:
            active_ics.append((ic["sym"], ic["expiry"], ic["strikes"]))

    return len(active_ics), active_ics
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

    # 2026-09-25: VIDAR-scoped cap. Count only VIDAR-owned ICs (via
    # audit-log walk cross-referenced with broker positions). Previously
    # this counted ALL SPY positions, which meant other strategies'
    # positions or manual test probes would block VIDAR from opening —
    # exactly the kind of cross-strategy interference the operator wants
    # to prevent. Now `MAX_VIDAR_ICS` is checked against VIDAR-owned
    # positions only.
    vidar_open_count, _active_ics = _count_vidar_open_ics(b, AUDIT_LOG)
    if vidar_open_count >= MAX_VIDAR_ICS:
        _log(f"  ! VIDAR cap reached ({vidar_open_count} VIDAR-owned IC(s), "
             f"cap={MAX_VIDAR_ICS}). Skipping open.")
        _audit({
            "stage": "vidar_open_refused",
            "underlying": "SPY",
            "skip_reason": f"vidar_open_count={vidar_open_count}>=cap={MAX_VIDAR_ICS}",
            "tag": "vidar.idx",
            "scope": "vidar",
        })
        return 0

    # Apply mid × 1.02 to all 4 legs (mirrors place_vertical.py safety)
    # then round to the SPY options 0.05 tick grid (Tiger rejects with
    # "tick size: 0.01" when a limit isn't on the grid; the message is
    # misleading — the actual SPY tick is 0.05 for sub-$3 strikes).
    # See references/tiger-occ-tick-grid-2026-09-24.md (forthcoming).
    legs = p["mids"]
    _TICK = 0.05

    def _round_tick(price: float, tick: float = _TICK) -> float:
        return round(price / tick) * tick

    short_put_limit = _round_tick(legs["short_put"] * 1.02)
    long_put_limit = _round_tick(legs["long_put"] * 1.02)
    short_call_limit = _round_tick(legs["short_call"] * 1.02)
    long_call_limit = _round_tick(legs["long_call"] * 1.02)

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
        _log(f"  ✓ placed: order_id={getattr(result, 'order_id', '?')} status={getattr(result, 'status', '?')} message={getattr(result, 'message', '?')[:200]!r}")
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
            if _is_vidar_auto_close_active():
                _close_vidar_position(b, pos, dec)
            else:
                _log(f"  → auto-close INACTIVE (VIDAR_AUTO_CLOSE / paper-mode gate); "
                     f"would close {pos.underlying} {pos.expiry} ({dec.severity})")
    return 0


def _is_vidar_auto_close_active() -> bool:
    """Whether phase_exit_review should auto-close VIDAR positions.

    Rules:
      1. If VIDAR_AUTO_CLOSE=0 explicitly -> OFF (kill switch)
      2. If VIDAR_AUTO_CLOSE=1 explicitly -> ON (override)
      3. Else: ON if account_type=paper, OFF if account_type=live
    """
    explicit = os.environ.get("VIDAR_AUTO_CLOSE", "").strip().lower()
    if explicit in ("0", "false", "off", "no"):
        return False
    if explicit in ("1", "true", "on", "yes"):
        return True
    account_type = os.environ.get("TIGER_ACCOUNT_TYPE", "paper").lower()
    return account_type == "paper"


def _close_vidar_position(b, pos, dec) -> int:
    """Close a VIDAR iron condor by submitting 4 single-leg close orders.

    Close side reversal vs open:
      - Short legs: BUY to close
      - Long  legs: SELL to close
    Limits: BUY uses mid x 1.02, SELL uses mid x 0.98 (1-tick edge).
    Audit tag: stage=vidar_exit_auto_close, setup_reason=auto_close_exit_review.
    """
    _log(f"  -> AUTO-CLOSE VIDAR {pos.underlying} {pos.expiry} ({dec.severity}: {dec.reason})")
    try:
        from tigeropen.trade.domain.order import Order
        from tigeropen.common.util.contract_utils import option_contract
    except ImportError as exc:
        _log(f"  ! tigeropen import failed: {exc}")
        return 1

    legs = [
        ("short_call", pos.short_call_strike, "C", "BUY"),
        ("long_call",  pos.long_call_strike,  "C", "SELL"),
        ("short_put",  pos.short_put_strike,  "P", "BUY"),
        ("long_put",   pos.long_put_strike,   "P", "SELL"),
    ]

    # Build OCC identifiers (SPY  YYMMDD{Right}{Strike*1000 padded 8)
    try:
        exp_dt = datetime.strptime(pos.expiry, "%Y-%m-%d").date()
    except Exception as exc:
        _log(f"  ! bad expiry {pos.expiry}: {exc}")
        return 1
    exp_str = exp_dt.strftime("%y%m%d")
    sym_padded = pos.underlying.ljust(6)[:6]

    submitted = 0
    for leg_name, strike, right, side in legs:
        strike_padded = f"{int(round(float(strike) * 1000)):08d}"
        identifier = f"{sym_padded}{exp_str}{right}{strike_padded}"
        # Get mid price from broker
        try:
            contract = option_contract(identifier)
            quote = b._trade_client.get_quote(contract) if hasattr(b._trade_client, "get_quote") else None
            mid = float(getattr(quote, "latest_price", 0) or 0) if quote else 0
        except Exception:
            mid = 0
        if mid <= 0:
            _log(f"  ! {leg_name} {identifier} no mid price, skipping")
            continue
        limit = round(mid * (1.02 if side == "BUY" else 0.98), 2)
        try:
            order = Order(
                account=b.config.account_id,
                contract=contract,
                action=side,
                order_type="LMT",
                quantity=pos.quantity,
                limit_price=limit,
                time_in_force="GTC",
            )
            result = b._trade_client.place_order(order)
            order_id = str(getattr(result, "order_id", "?"))
            _log(f"    {side} {pos.quantity} {pos.underlying} {strike}{right} @ {limit} -> order_id={order_id}")
            submitted += 1
            _audit({
                "stage": "vidar_exit_auto_close",
                "underlying": pos.underlying,
                "expiry": pos.expiry,
                "leg": leg_name,
                "strike": strike,
                "right": right,
                "quantity": pos.quantity,
                "side": side,
                "limit_price": limit,
                "exit_reason": dec.severity,
                "rationale": dec.reason,
                "order_id": order_id,
                "tag": "vidar.idx",
                "scope": "vidar",
                "setup_reason": "auto_close_exit_review",
            })
        except Exception as exc:
            _log(f"  ! close order failed for {leg_name}: {exc}")
    _log(f"  -> AUTO-CLOSE done: {submitted}/4 legs submitted")
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
