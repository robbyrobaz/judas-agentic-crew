"""Standing NT-truth orphan reconcile (2026-07-16).

The recurring 2026-07 emergency: orphaned OCO legs open NT positions with no DB
row, and nothing in the scan caught them (a prior autofix shipped this as a
`return 0` stub). This verifies the real body flattens unmanaged positions,
leaves managed ones alone, and skips expired-contract phantoms.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _setup(monkeypatch, nt_positions, managed_trades):
    import src.portfolio_runtime as pr
    import src.research.agent_tools as at
    from src.db.models import init_db, get_conn

    db = tempfile.mktemp(suffix=".db")
    init_db(db)
    with get_conn(db) as c:
        for sym, direction in managed_trades:
            c.execute(
                "INSERT INTO trades (symbol,direction,qty,entry_fill,status,opened_at) "
                "VALUES (?,?,1,1,'open','2026-07-16T00:00:00Z')", (sym, direction))

    monkeypatch.setattr(at, "get_nt_positions",
                        lambda **k: {"ok": True, "flat": False, "open_positions": nt_positions})
    calls = []

    class FakeBroker:
        instrument_map = {"MGC": "MGC 08-26", "MBT": "MBT 07-26", "MET": "MET 07-26"}
        def flatten(self, sym, *, direction, quantity):
            calls.append((sym, direction, quantity)); return 1.0

    monkeypatch.setattr(pr, "_nt_broker", lambda: FakeBroker())
    return pr, db, calls


def test_flattens_unmanaged_leaves_managed(monkeypatch):
    pr, db, calls = _setup(monkeypatch,
        nt_positions=[
            {"instrument": "MGC", "side": "LONG", "qty": 1, "last_exec_utc": "2026-07-16T13:00:00.123456"},
            {"instrument": "MBT", "side": "SHORT", "qty": 3, "last_exec_utc": "2026-07-16T11:00:00.5"},
        ],
        managed_trades=[("MGC", "long")])
    assert pr._reconcile_nt_orphans(db) == 1
    assert calls == [("MBT", "short", 3)]  # only the orphan


def test_skips_expired_phantom(monkeypatch):
    pr, db, calls = _setup(monkeypatch,
        nt_positions=[{"instrument": "MET", "side": "LONG", "qty": 1, "last_exec_utc": "2026-06-19T17:06:00"}],
        managed_trades=[])
    assert pr._reconcile_nt_orphans(db) == 0  # 4 weeks old = expired, skipped
    assert calls == []


def test_flat_is_noop(monkeypatch):
    pr, db, calls = _setup(monkeypatch, nt_positions=[], managed_trades=[])
    import src.research.agent_tools as at
    monkeypatch.setattr(at, "get_nt_positions", lambda **k: {"ok": True, "flat": True, "open_positions": []})
    assert pr._reconcile_nt_orphans(db) == 0


def test_cooldown_prevents_double_flatten(monkeypatch):
    """A symbol flattened <20 min ago must NOT be flattened again — the stale
    sync would otherwise show it still open and we'd flip it."""
    pr, db, calls = _setup(monkeypatch,
        nt_positions=[{"instrument": "MBT", "side": "SHORT", "qty": 3, "last_exec_utc": "2026-07-16T11:00:00.5"}],
        managed_trades=[])
    assert pr._reconcile_nt_orphans(db) == 1   # first pass flattens
    assert pr._reconcile_nt_orphans(db) == 0   # second pass: cooldown blocks it
    assert calls == [("MBT", "short", 3)]      # only ONCE


def test_flattens_excess_over_managed_qty(monkeypatch):
    """NT short 2, DB manages short 1 → flatten only the 1 EXCESS contract."""
    pr, db, calls = _setup(monkeypatch,
        nt_positions=[{"instrument": "MET", "side": "SHORT", "qty": 2, "last_exec_utc": "2026-07-16T14:00:00.1"}],
        managed_trades=[("MET", "short")])   # 1 managed
    assert pr._reconcile_nt_orphans(db) == 1
    assert calls == [("MET", "short", 1)]    # excess 2-1=1


def test_fully_managed_qty_left_alone(monkeypatch):
    pr, db, calls = _setup(monkeypatch,
        nt_positions=[{"instrument": "MCL", "side": "LONG", "qty": 1, "last_exec_utc": "2026-07-16T14:00:00.1"}],
        managed_trades=[("MCL", "long")])
    assert pr._reconcile_nt_orphans(db) == 0
    assert calls == []
