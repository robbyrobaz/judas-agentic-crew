"""Regression test for the researcher briefing burnout section.

Bug: _build_kickoff() in researcher_agent.py queried auto_demotions
with `retired_at_utc >= ...`, but that table has no such column.
The correct column is `ts_utc` (the demotion timestamp). The broken
query caused the entire briefing to print
"[context load error: no such column: retired_at_utc]" at the end,
silently swallowing the rest of the briefing work above the
try/except.

This test ensures that when the auto_demotions table has rows in the
last 7 days, the briefing surfaces them under the BURNOUT ALERT
section instead of crashing.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

from src.db.models import init_db
from src.research.researcher_agent import _build_kickoff


def _insert_active_strategy(conn, symbol, family, params=None):
    if params is None:
        params = {"k": "v"}
    cur = conn.execute(
        "INSERT INTO active_strategies (symbol, strategy_family, version, params_json, state) "
        "VALUES (?, ?, 1, ?, 'active')",
        (symbol, family, json.dumps(params)),
    )
    return cur.lastrowid


def _insert_demotion(conn, strategy_id, symbol, family, ts_utc):
    conn.execute(
        "INSERT INTO auto_demotions "
        "(ts_utc, strategy_id, symbol, strategy_family, version, params_json, metrics_snapshot_json, reason) "
        "VALUES (?, ?, ?, ?, 1, '{}', '{}', 'test')",
        (ts_utc, strategy_id, symbol, family),
    )


def test_burnout_summary_surfaces_recent_demotions(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # Two demotions for MBT custom_5m in the last 7 days.
    sid = _insert_active_strategy(conn, "MBT", "custom_5m")
    now = datetime.now(timezone.utc)
    for hours_ago in (1, 24):
        ts = (now - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_demotion(conn, sid, "MBT", "custom_5m", ts)
    conn.commit()

    brief = _build_kickoff(db)
    conn.close()

    # The fixed query should surface the burnout alert, NOT crash.
    assert "BURNOUT ALERT" in brief, brief
    assert "MBT custom_5m" in brief
    # The pre-existing try/except wrapper used to swallow the column
    # error; the briefing must no longer print the column-error tag.
    assert "no such column: retired_at_utc" not in brief, brief


def test_burnout_summary_ignores_old_demotions(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # One demotion 30 days ago — should NOT appear (outside 7d window).
    sid = _insert_active_strategy(conn, "MBT", "custom_5m")
    ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_demotion(conn, sid, "MBT", "custom_5m", ts)
    conn.commit()

    brief = _build_kickoff(db)
    conn.close()

    # No burnout alert should fire (only 1 demotion, and it's > 7d old).
    assert "BURNOUT ALERT" not in brief
    assert "no such column: retired_at_utc" not in brief
