"""Tests for the VIDAR-only cap (_count_vidar_open_ics).

The previous implementation counted ALL SPY positions, which meant
other strategies' positions or manual test probes would block VIDAR
from opening — exactly the kind of cross-strategy interference the
operator wants to prevent (2026-09-25).

The new implementation walks the audit log and counts only VIDAR-owned
iron condors (those opened via `stage=vidar_open status=FILLED` that
haven't been offset by a close).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_audit(audit_path, events):
    """Write a list of audit events to the audit_path JSONL."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _vidar_open(sym="SPY", expiry="2026-10-30", sc=805, lc=810, sp=738, lp=733,
                 status="OrderStatus.FILLED"):
    return {
        "stage": "vidar_open",
        "strategy": "vidar",
        "underlying": sym,
        "expiry": expiry,
        "short_put_strike": sp,
        "short_call_strike": sc,
        "long_put_strike": lp,
        "long_call_strike": lc,
        "status": status,
        "quantity": 1,
    }


def _vidar_close(sym="SPY", expiry="2026-10-30", strike=805):
    return {
        "stage": "vidar_close",
        "strategy": "vidar",
        "underlying": sym,
        "expiry": expiry,
        "strike": strike,
        "quantity": 1,
    }


def _broker_expired(sym="SPY", expiry="2026-10-30", strike=805):
    return {
        "stage": "broker_expired",
        "strategy": "vidar",
        "underlying": sym,
        "expiry": expiry,
        "strike": strike,
    }


def _other_strategy_open(sym="SPY", expiry="2026-10-30", strike=700):
    """RAGNAR/FREYA/DEZ opened an SPY position. Should NOT count toward VIDAR cap."""
    return {
        "stage": "dez_open",
        "strategy": "dez",
        "underlying": sym,
        "expiry": expiry,
        "strike": strike,
        "quantity": 1,
    }


def test_zero_vidar_ics_when_no_events(tmp_path):
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    count, active = _count_vidar_open_ics(audit)
    assert count == 0
    assert active == []


def test_one_vidar_ic_no_close(tmp_path):
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    _make_audit(audit, [_vidar_open()])
    count, active = _count_vidar_open_ics(audit)
    assert count == 1
    assert len(active) == 1
    assert active[0][0] == "SPY"


def test_vidar_ic_with_full_close_drops_count(tmp_path):
    """An IC closed via 4 leg closes (or one combo close) shouldn't count."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [_vidar_open()]
    # Close each leg separately
    events += [_vidar_close(strike=s) for s in [805, 810, 738, 733]]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 0


def test_two_vidar_ics_open(tmp_path):
    """Two open vidar ICs should count as 2."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [
        _vidar_open(expiry="2026-10-30", sc=805, lc=810, sp=738, lp=733),
        _vidar_open(expiry="2026-11-20", sc=805, lc=810, sp=738, lp=733),
    ]
    _make_audit(audit, events)
    count, active = _count_vidar_open_ics(audit)
    assert count == 2
    assert len(active) == 2


def test_other_strategy_positions_dont_count_toward_vidar_cap(tmp_path):
    """DEZ/RAGNAR/FREYA SPY positions must not block VIDAR's cap check.

    This is the operator's 2026-09-25 requirement: cap should be
    VIDAR-scoped so other strategies / testing don't interfere with
    VIDAR's intent.
    """
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    # 3 SPY positions from other strategies (DEZ, RAGNAR.vert, manual)
    events = [
        _other_strategy_open(strike=700),
        _other_strategy_open(strike=705),
        _other_strategy_open(strike=710),
    ]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 0, (
        "VIDAR cap must NOT count other strategies' SPY positions. "
        "Found 0 VIDAR ICs but other-strategy positions exist — the "
        "cap should let VIDAR open here."
    )


def test_rejected_vidar_open_doesnt_count(tmp_path):
    """vidar_open with status=REJECTED shouldn't count (broker rejected)."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [_vidar_open(status="OrderStatus.REJECTED")]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 0


def test_pending_vidar_open_does_count(tmp_path):
    """vidar_open with status=PENDING (broker accepted, awaiting fill)
    should still count — the cron doesn't poll fill status, so PENDING
    in the audit log means the order MIGHT have filled since.
    Conservative: count it as open."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [_vidar_open(status="OrderStatus.PENDING")]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 1, (
        "PENDING vidar_open events should count as open — cron doesn't "
        "update the audit log after fill, so PENDING might actually be FILLED."
    )


def test_vidar_open_refused_doesnt_count(tmp_path):
    """vidar_open_refused is the cap-block event itself, not an open."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [
        {"stage": "vidar_open_refused", "strategy": "vidar",
         "underlying": "SPY", "expiry": "2026-10-30"},
    ]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 0


def test_broker_expired_closes_vidar_legs(tmp_path):
    """broker_expired events should decrement the VIDAR leg counts."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [_vidar_open()]
    # All 4 legs expire
    events += [_broker_expired(strike=s) for s in [805, 810, 738, 733]]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 0


def test_partial_close_only_drops_count_when_all_legs_closed(tmp_path):
    """An IC is only 'closed' when ALL 4 legs are closed.

    3 of 4 legs closed → IC still counts as 1 open (orphan leg).
    """
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [_vidar_open()]
    # Only 3 legs closed
    events += [_vidar_close(strike=s) for s in [805, 810, 738]]
    _make_audit(audit, events)
    count, _ = _count_vidar_open_ics(audit)
    assert count == 1, (
        "IC with 1 orphan leg should still count as 1 open IC "
        "(the stranded-leg detector should flag the orphan separately)"
    )


def test_mixed_strategies_with_one_vidar_ic(tmp_path):
    """Other strategies + 1 vidar IC = count=1 (only VIDAR counts)."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    audit = tmp_path / "audit.jsonl"
    events = [
        _other_strategy_open(strike=700),
        _other_strategy_open(strike=705),
        _vidar_open(),
    ]
    _make_audit(audit, events)
    count, active = _count_vidar_open_ics(audit)
    assert count == 1, "Should count only VIDAR IC, ignoring other strategies"
    assert active[0][0] == "SPY"
