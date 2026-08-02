"""Regression: _check_position_protection reconciles zombies even when SL/TP orders
remain "working" in NT (the bf6a73e5 + cycle 2026-07-27T03Z pattern, trades #448/#449).

The OLD ordering checked stop_live first and `continue`d before the position
query, so a DB-open trade whose NT position was already FLAT (closed externally
or rolled) but whose SL/TP orders were still "working" was masked forever.

The NEW ordering checks the position first:
  - p == 0          -> cancel orphan SL/TP, close zombie, continue
  - p is None       -> bad read, skip silently
  - p != 0 and stop -> continue (protected)
  - p != 0 and !stop -> re-arm / flatten / naked as before
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _setup(tmp_db, nt_position=0.0, sl_status="Working", open_trade=None):
    """Insert one open trade and patch the NT broker so the scan sees the
    specified position + SL status."""
    from src.db.models import init_db, get_conn

    init_db(tmp_db)
    if open_trade is None:
        open_trade = dict(
            symbol="MCL", direction="long", qty=1, entry_fill=84.62,
            stop_price=83.18, target_price=87.51,
            sl_order_id="SL-ORPHAN-1", tp_order_id="TP-ORPHAN-1",
            opened_at="2026-07-26T22:41:31Z",
        )
    with get_conn(tmp_db) as c:
        c.execute(
            "INSERT INTO trades (symbol,direction,qty,entry_fill,stop_price,"
            "target_price,sl_order_id,tp_order_id,opened_at,status) "
            "VALUES (:symbol,:direction,:qty,:entry_fill,:stop_price,"
            ":target_price,:sl_order_id,:tp_order_id,:opened_at,'open')",
            open_trade,
        )

    import src.portfolio_runtime as pr
    broker = MagicMock()
    broker.account = "TEST"
    broker._resolve_instrument.return_value = "MCL 09-26"
    # _nt_run returns lines in the format the scan parses
    lines = []
    tid = 1
    if nt_position is not None:
        lines.append(f'P:{tid}:{nt_position}')
    lines.append(f'S:{tid}:{sl_status}')
    broker._nt_run.return_value = (0, "\n".join(lines) + "\n")
    broker.cancel.return_value = True  # best-effort success

    pr._nt_broker = lambda: broker  # type: ignore[assignment]
    return pr, tmp_db, broker


def test_zombie_closed_when_position_flat_even_if_sl_working(tmp_path):
    """p=0 + sl='Working' must close the zombie (regression for #448/#449)."""
    db = str(tmp_path / "judas.db")
    pr, db, broker = _setup(db, nt_position=0.0, sl_status="Working")

    pr._check_position_protection(db)

    # Trade must be closed in DB
    from src.db.models import get_conn
    with get_conn(db) as c:
        row = c.execute(
            "SELECT status, exit_reason FROM trades WHERE id=1"
        ).fetchone()
    assert row["status"] == "closed", f"expected closed, got {row['status']!r}"
    assert row["exit_reason"] in ("reconciled_position_flat", "manual_close", "stop", "target")

    # Orphan SL + TP orders must have been cancelled (best-effort)
    assert broker.cancel.call_count == 2
    cancelled = [call.args[0] for call in broker.cancel.call_args_list]
    assert "SL-ORPHAN-1" in cancelled
    assert "TP-ORPHAN-1" in cancelled


def test_zombie_closed_when_position_flat_and_sl_cancelled(tmp_path):
    """p=0 + sl='Cancelled' (the simpler case) must also close the zombie."""
    db = str(tmp_path / "judas.db")
    pr, db, broker = _setup(db, nt_position=0.0, sl_status="Cancelled")

    pr._check_position_protection(db)

    from src.db.models import get_conn
    with get_conn(db) as c:
        row = c.execute("SELECT status FROM trades WHERE id=1").fetchone()
    assert row["status"] == "closed"


def test_zombie_left_alone_when_position_query_fails(tmp_path):
    """p=None (bad read) must NOT close — old behavior preserved."""
    db = str(tmp_path / "judas.db")
    # sl_status is irrelevant when p is None; pass Working to match the case
    pr, db, broker = _setup(db, nt_position=None, sl_status="Working")

    pr._check_position_protection(db)

    from src.db.models import get_conn
    with get_conn(db) as c:
        row = c.execute("SELECT status FROM trades WHERE id=1").fetchone()
    assert row["status"] == "open", "p=None must not trigger zombie close"
    # No cancel calls either — we never got far enough
    assert broker.cancel.call_count == 0


def test_position_protected_left_alone_when_stop_working(tmp_path):
    """p=1 + sl='Working' (position live + stop live) -> leave alone, NO cancel."""
    db = str(tmp_path / "judas.db")
    pr, db, broker = _setup(db, nt_position=1.0, sl_status="Working")

    pr._check_position_protection(db)

    from src.db.models import get_conn
    with get_conn(db) as c:
        row = c.execute("SELECT status FROM trades WHERE id=1").fetchone()
    assert row["status"] == "open", "protected position must stay open"
    assert broker.cancel.call_count == 0


def test_no_opens_returns_empty_naked_file(tmp_path, monkeypatch):
    """No open trades -> empty naked_positions.json written, no crash."""
    db = str(tmp_path / "judas.db")
    from src.db.models import init_db
    init_db(db)

    import src.portfolio_runtime as pr
    pr._nt_broker = lambda: MagicMock()  # type: ignore[assignment]

    monkeypatch.setattr(pr, "_STATUS_DIR", tmp_path)
    result = pr._check_position_protection(db)
    assert result == []
    naked_file = tmp_path / "naked_positions.json"
    assert naked_file.exists()
    import json
    assert json.loads(naked_file.read_text()) == {"ts": None, "naked": []}