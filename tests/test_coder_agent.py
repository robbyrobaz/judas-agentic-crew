"""Coder specialist regressions."""
from __future__ import annotations

import sqlite3

from src.db.models import init_db
from src.research import coder_agent, agent_tools


def test_inhibit_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_CODER_AGENT_INHIBIT", "1")
    db = str(tmp_path / "j.db")
    init_db(db)
    out = coder_agent.run_coder_decision(db_path=db)
    assert out.fallback_used is True


def test_no_open_tasks_returns_clean(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDAS_CODER_AGENT_INHIBIT", raising=False)
    db = str(tmp_path / "j.db")
    init_db(db)
    out = coder_agent.run_coder_decision(db_path=db)
    assert out.success is True
    assert out.actions_taken == []


def test_processes_queued_coder_task(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDAS_CODER_AGENT_INHIBIT", raising=False)
    # Inhibit the actual autofix dispatch (Phase 3 harness call).
    monkeypatch.setenv("JUDAS_AUTOFIX_INHIBIT", "1")

    db = str(tmp_path / "j.db")
    init_db(db)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="coder", action="autofix_symptom",
              payload={"symptom": "research loop hangs",
                       "context": "see logs"},
              rationale="research loop hangs")["task_id"]

    out = coder_agent.run_coder_decision(db_path=db)
    assert out.success is True
    assert len(out.actions_taken) == 1

    # Task should be marked done.
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT status FROM agent_tasks WHERE id=?",
                        (tid,)).fetchone()
    assert row["status"] == "done"

    # An auto_fixes row must have been recorded.
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM auto_fixes").fetchone()[0]
    assert n == 1
