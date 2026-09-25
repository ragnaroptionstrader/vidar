"""Tests for the VIDAR-only cap (_count_vidar_open_ics).

The previous implementation counted ALL SPY positions, which meant
other strategies' positions or manual test probes would block VIDAR
from opening — exactly the kind of cross-strategy interference the
operator wants to prevent (2026-09-25).

The new implementation walks the audit log AND cross-references with
current broker positions. A vidar_open event counts as "actually
open" only if at least one of its 4 strikes has a current broker
position. This correctly handles:
  - cron orders that were REJECTED before filling (audit says
    PENDING but broker has no position)
  - cron orders that DID fill (audit + broker both reflect)
  - manual closes (broker has no position even though audit says open)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_audit(audit_path, events):
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


def _make_fake_broker(legs):
    """Build a minimal broker-like object with positions.

    `legs` is a list of (sym, expiry, strike) tuples — the current
    broker position state.
    """
    class FakeContract:
        def __init__(self, sym, expiry, strike):
            self.symbol = sym
            self.expiry = expiry
            self.strike = strike

    class FakePosition:
        def __init__(self, sym, expiry, strike):
            self.contract = FakeContract(sym, expiry, strike)

    class FakeBroker:
        def __init__(self, legs):
            self.legs = legs
            self._trade_client = self

        def get_positions(self, sec_type=None):
            return [FakePosition(s, e, st) for (s, e, st) in self.legs]

    return FakeBroker(legs)


def test_zero_vidar_ics_when_no_events():
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    broker = _make_fake_broker([])
    count, active = _count_vidar_open_ics(broker, audit_path)
    assert count == 0
    assert active == []


def test_vidar_open_with_no_broker_position_doesnt_count():
    """A vidar_open audit event with status=PENDING but NO broker
    position must NOT count — the cron submitted but the order was
    rejected/expired before filling. Caught 2026-09-25: 3 such stale
    vidar_open events would have over-counted the cap."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [
        _vidar_open(status="OrderStatus.PENDING"),
    ]
    _make_audit(audit_path, events)
    # No broker positions — the cron order never filled
    broker = _make_fake_broker([])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0, (
        "vidar_open with no broker position must not count — the "
        "audit log status is stale (PENDING means broker accepted, not "
        "filled). The broker_legs cross-reference is the truth."
    )


def test_one_vidar_ic_with_broker_position_counts():
    """If at least one of the IC's strikes is in the broker, the IC
    counts as open."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [_vidar_open(sc=805, lc=810, sp=738, lp=733)]
    _make_audit(audit_path, events)
    # All 4 legs in broker (full IC filled)
    broker = _make_fake_broker([
        ("SPY", "2026-10-30", 805),
        ("SPY", "2026-10-30", 810),
        ("SPY", "2026-10-30", 738),
        ("SPY", "2026-10-30", 733),
    ])
    count, active = _count_vidar_open_ics(broker, audit_path)
    assert count == 1


def test_ic_with_orphan_leg_still_counts():
    """If 3 legs are closed but 1 is still in broker, the IC is
    still 'actually open' — operator shouldn't be able to stack."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [_vidar_open()]
    _make_audit(audit_path, events)
    # Only 1 leg remains in broker
    broker = _make_fake_broker([("SPY", "2026-10-30", 733)])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 1


def test_ic_fully_closed_doesnt_count():
    """If all 4 legs are closed AND broker has no positions for any
    strike, the IC drops from the count."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [_vidar_open()]
    events += [_vidar_close(strike=s) for s in [805, 810, 738, 733]]
    _make_audit(audit_path, events)
    broker = _make_fake_broker([])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0


def test_other_strategy_positions_dont_count_toward_vidar_cap():
    """RAGNAR/FREYA/DEZ/manual SPY positions must NOT block VIDAR's cap.
    Even though they're SPY, they have no matching vidar_open audit event.
    """
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    # No vidar_open events in audit log
    events = [
        {"stage": "dez_open", "strategy": "dez",
         "underlying": "SPY", "expiry": "2026-10-30",
         "strike": 700, "quantity": 1},
        {"stage": "broker_trade", "strategy": "ragnar.vert",
         "underlying": "SPY", "expiry": "2026-10-30",
         "strike": 705, "quantity": 1},
    ]
    _make_audit(audit_path, events)
    # Broker has 3 SPY positions from other strategies
    broker = _make_fake_broker([
        ("SPY", "2026-10-30", 700),
        ("SPY", "2026-10-30", 705),
        ("SPY", "2026-10-30", 710),
    ])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0, (
        "VIDAR cap must NOT count other strategies' SPY positions. "
        "Cap should let VIDAR open here — no vidar_open events."
    )


def test_rejected_vidar_open_doesnt_count():
    """vidar_open with status=REJECTED means broker refused — shouldn't
    count even if a stale broker position happens to match."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [_vidar_open(status="OrderStatus.REJECTED")]
    _make_audit(audit_path, events)
    broker = _make_fake_broker([
        ("SPY", "2026-10-30", 805),
        ("SPY", "2026-10-30", 810),
        ("SPY", "2026-10-30", 738),
        ("SPY", "2026-10-30", 733),
    ])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0


def test_vidar_open_refused_doesnt_count():
    """vidar_open_refused is the cap-block event, not an open."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [
        {"stage": "vidar_open_refused", "strategy": "vidar",
         "underlying": "SPY", "expiry": "2026-10-30"},
    ]
    _make_audit(audit_path, events)
    broker = _make_fake_broker([("SPY", "2026-10-30", 805)])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0


def test_broker_expired_closes_vidar_legs():
    """broker_expired events should decrement the VIDAR leg counts."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [_vidar_open()]
    events += [_broker_expired(strike=s) for s in [805, 810, 738, 733]]
    _make_audit(audit_path, events)
    broker = _make_fake_broker([])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0


def test_two_vidar_ics_open():
    """Two open vidar ICs should count as 2."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    events = [
        _vidar_open(expiry="2026-10-30", sc=805, lc=810, sp=738, lp=733),
        _vidar_open(expiry="2026-11-20", sc=805, lc=810, sp=738, lp=733),
    ]
    _make_audit(audit_path, events)
    broker = _make_fake_broker([
        # First IC all 4 legs
        ("SPY", "2026-10-30", 805), ("SPY", "2026-10-30", 810),
        ("SPY", "2026-10-30", 738), ("SPY", "2026-10-30", 733),
        # Second IC all 4 legs
        ("SPY", "2026-11-20", 805), ("SPY", "2026-11-20", 810),
        ("SPY", "2026-11-20", 738), ("SPY", "2026-11-20", 733),
    ])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 2


def test_cron_audit_pending_no_broker_position_is_zero():
    """The exact 2026-09-25 case: vidar_open in audit log, but the
    order never filled at the broker. Cap should NOT block."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    # 3 vidar_open events from cron attempts (all PENDING but never filled)
    events = [
        _vidar_open(expiry="2026-10-23", sc=802, lc=807, sp=741, lp=736,
                    status="OrderStatus.PENDING"),
        _vidar_open(expiry="2026-10-30", sc=805, lc=810, sp=738, lp=733,
                    status="OrderStatus.PENDING"),
        _vidar_open(expiry="2026-10-30", sc=798, lc=803, sp=730, lp=725,
                    status="OrderStatus.PENDING"),
    ]
    _make_audit(audit_path, events)
    # Broker has only the manual-probe positions (different strikes)
    broker = _make_fake_broker([
        ("SPY", "2026-10-30", 799),
        ("SPY", "2026-10-30", 804),
        ("SPY", "2026-10-30", 726),
        ("SPY", "2026-10-30", 731),
    ])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0, (
        "Stale audit events without broker positions must NOT count. "
        "This is the exact bug from 2026-09-25 — cron submitted 3 ICs, "
        "none filled, but my code counted all 3 as 'open'."
    )


def test_vidar_open_update_rejected_overrides_pending():
    """A vidar_open with status=PENDING followed by a vidar_open_update
    with status=REJECTED for the same order_id should NOT count as open.
    The post-placement poll (added 2026-09-25) catches cases where the
    broker rejected/expired the order after the initial PENDING log."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    order_id = "44746684098430976"
    events = [
        {**_vidar_open(sc=798, lc=803, sp=730, lp=725, status="OrderStatus.PENDING"),
         "order_id": order_id},
        {"stage": "vidar_open_update", "strategy": "vidar",
         "underlying": "SPY", "expiry": "2026-10-30",
         "order_id": order_id,
         "initial_status": "OrderStatus.PENDING",
         "status": "OrderStatus.REJECTED"},
    ]
    _make_audit(audit_path, events)
    # No broker positions — the order was rejected
    broker = _make_fake_broker([])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 0


def test_vidar_open_update_filled_keeps_count():
    """A vidar_open with status=PENDING followed by a vidar_open_update
    with status=FILLED for the same order_id should still count as open
    if broker has matching positions."""
    from ragnar_scripts.vidar_auto import _count_vidar_open_ics
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write("")
        audit_path = Path(f.name)
    order_id = "44747387577583616"
    events = [
        {**_vidar_open(sc=798, lc=803, sp=730, lp=725, status="OrderStatus.PENDING"),
         "order_id": order_id},
        {"stage": "vidar_open_update", "strategy": "vidar",
         "underlying": "SPY", "expiry": "2026-10-30",
         "order_id": order_id,
         "initial_status": "OrderStatus.PENDING",
         "status": "OrderStatus.FILLED",
         "filled_quantity": 1, "filled_price": 1.06},
    ]
    _make_audit(audit_path, events)
    # Broker has the matching positions
    broker = _make_fake_broker([
        ("SPY", "2026-10-30", 798),
        ("SPY", "2026-10-30", 803),
        ("SPY", "2026-10-30", 730),
        ("SPY", "2026-10-30", 725),
    ])
    count, _ = _count_vidar_open_ics(broker, audit_path)
    assert count == 1


def test_poll_vidar_order_status_returns_filled():
    """Smoke test the poll helper — it should return an OrderResult
    when get_order returns FILLED on first try."""
    from ragnar_scripts.vidar_auto import _poll_vidar_order_status

    class FakeResult:
        def __init__(self, status):
            self.status = status
            self.filled_quantity = 1
            self.filled_price = 1.0

    class FakeBroker:
        def __init__(self):
            self.calls = 0
        def get_order(self, order_id):
            self.calls += 1
            return FakeResult("OrderStatus.FILLED")

    b = FakeBroker()
    r = _poll_vidar_order_status(b, "12345", max_wait_sec=10)
    assert r is not None
    assert str(r.status) == "OrderStatus.FILLED"
    assert b.calls == 1


def test_poll_vidar_order_status_returns_none_on_timeout():
    """Poll helper returns None if get_order keeps returning PENDING
    past the timeout."""
    from ragnar_scripts.vidar_auto import _poll_vidar_order_status

    class FakeResult:
        def __init__(self, status):
            self.status = status
            self.filled_quantity = 0
            self.filled_price = 0.0

    class FakeBroker:
        def get_order(self, order_id):
            return FakeResult("OrderStatus.PENDING")

    b = FakeBroker()
    r = _poll_vidar_order_status(b, "12345", max_wait_sec=1)
    assert r is None  # timed out without terminal status


def test_poll_vidar_order_status_handles_exception():
    """Poll helper retries on exception (e.g. broker network blip)."""
    from ragnar_scripts.vidar_auto import _poll_vidar_order_status

    class FakeResult:
        def __init__(self, status):
            self.status = status
            self.filled_quantity = 0
            self.filled_price = 0.0

    class FakeBroker:
        def __init__(self):
            self.calls = 0
        def get_order(self, order_id):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("transient")
            return FakeResult("OrderStatus.FILLED")

    b = FakeBroker()
    r = _poll_vidar_order_status(b, "12345", max_wait_sec=30)
    assert r is not None
    assert str(r.status) == "OrderStatus.FILLED"
    assert b.calls == 2
