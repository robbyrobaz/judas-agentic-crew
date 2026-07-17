"""Standing NT-truth orphan reconcile (2026-07-16, rewritten 2026-07-17).

The recurring 2026-07 emergency: orphaned OCO legs open NT positions with no DB
row, and nothing in the scan caught them (a prior autofix shipped this as a
`return 0` stub).

2026-07-17 change: the reconcile no longer AUTO-FLATTENS. Auto-flatten would dump
genuinely profitable unmanaged positions the moment the (now-live) feed could see
them — SimJudasCrew was +$5k on ~10 unmanaged contracts. Per Rob's mandate the LLM
crew manages positions, so the scan now DETECTS unmanaged contracts and QUEUES a
high-urgency `reconcile_unmanaged_positions` task for the trader team. These tests
verify detection, the managed/excess math, and task dedup.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _setup(monkeypatch, nt_positions, managed_trades):
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
    import src.portfolio_runtime as pr
    return pr, db


def _reconcile_tasks(db):
    from src.db.models import get_conn
    with get_conn(db) as c:
        rows = c.execute(
            "SELECT id, team, action, urgency, status, rationale FROM agent_tasks "
            "WHERE action='reconcile_unmanaged_positions' ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def test_detects_unmanaged_queues_task_leaves_managed(monkeypatch):
    pr, db = _setup(monkeypatch,
        nt_positions=[
            {"instrument": "MGC", "side": "LONG", "qty": 1, "contract": "MGC 08-26"},
            {"instrument": "MBT", "side": "SHORT", "qty": 3, "contract": "MBT 07-26"},
        ],
        managed_trades=[("MGC", "long")])
    # returns TOTAL unmanaged contracts (MBT's 3; MGC fully managed)
    assert pr._reconcile_nt_orphans(db) == 3
    tasks = _reconcile_tasks(db)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["team"] == "trader" and t["urgency"] == "high" and t["status"] == "open"
    assert "MBT" in t["rationale"] and "MGC" not in t["rationale"]  # only the orphan


def test_flat_is_noop(monkeypatch):
    import src.research.agent_tools as at
    pr, db = _setup(monkeypatch, nt_positions=[], managed_trades=[])
    monkeypatch.setattr(at, "get_nt_positions",
                        lambda **k: {"ok": True, "flat": True, "open_positions": []})
    assert pr._reconcile_nt_orphans(db) == 0
    assert _reconcile_tasks(db) == []


def test_detects_excess_over_managed_qty(monkeypatch):
    """NT short 2, DB manages short 1 → 1 EXCESS contract is unmanaged."""
    pr, db = _setup(monkeypatch,
        nt_positions=[{"instrument": "MET", "side": "SHORT", "qty": 2, "contract": "MET 07-26"}],
        managed_trades=[("MET", "short")])
    assert pr._reconcile_nt_orphans(db) == 1
    tasks = _reconcile_tasks(db)
    assert len(tasks) == 1 and "MET" in tasks[0]["rationale"]


def test_fully_managed_qty_left_alone(monkeypatch):
    pr, db = _setup(monkeypatch,
        nt_positions=[{"instrument": "MCL", "side": "LONG", "qty": 1, "contract": "MCL 08-26"}],
        managed_trades=[("MCL", "long")])
    assert pr._reconcile_nt_orphans(db) == 0
    assert _reconcile_tasks(db) == []


def test_dedup_refreshes_single_task(monkeypatch):
    """Two scans while a task is still open must not pile a second task — the
    existing one is refreshed in place so the crew sees one current queue item."""
    pr, db = _setup(monkeypatch,
        nt_positions=[{"instrument": "MBT", "side": "SHORT", "qty": 3, "contract": "MBT 07-26"}],
        managed_trades=[])
    assert pr._reconcile_nt_orphans(db) == 3
    assert pr._reconcile_nt_orphans(db) == 3
    assert len(_reconcile_tasks(db)) == 1   # still ONE task, refreshed not duplicated
