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

    # The autofix was INHIBITED, so no real fix landed (auto_fixes.status stays
    # 'detected', not 'completed'). The verification gate must therefore mark the
    # task 'failed' — NOT fake it as 'done'. (Jun-18: the old code marked it 'done'
    # regardless, hiding live bugs; that is the behavior this test now guards.)
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT status FROM agent_tasks WHERE id=?",
                        (tid,)).fetchone()
    assert row["status"] == "failed"

    # An auto_fixes row must still have been recorded (symptom captured).
    with sqlite3.connect(db) as c:
        n = c.execute("SELECT COUNT(*) FROM auto_fixes").fetchone()[0]
    assert n == 1


def test_completed_autofix_marks_task_done(tmp_path, monkeypatch):
    """Happy path: when the autofix actually lands (auto_fixes.status='completed'),
    the verification gate marks the task 'done'. Complements the inhibited case."""
    monkeypatch.delenv("JUDAS_CODER_AGENT_INHIBIT", raising=False)
    monkeypatch.delenv("JUDAS_AUTOFIX_INHIBIT", raising=False)

    db = str(tmp_path / "j.db")
    init_db(db)
    enq = agent_tools.make_enqueue_task(db_path=db, requester="operator")
    tid = enq(team="coder", action="autofix_symptom",
              payload={"symptom": "x", "context": "y"},
              rationale="x")["task_id"]

    # Simulate a successful autofix: mark the newest auto_fixes row 'completed'
    # instead of spinning up a real worktree + LLM harness.
    from src.flows.operator_flow import OperatorFlow

    def _fake_autofix(_self, *, db_path):
        with sqlite3.connect(db_path) as c:
            c.execute("UPDATE auto_fixes SET status='completed' "
                      "WHERE id=(SELECT MAX(id) FROM auto_fixes)")
            c.commit()

    monkeypatch.setattr(OperatorFlow, "_try_run_one_autofix", _fake_autofix)

    out = coder_agent.run_coder_decision(db_path=db)
    assert out.success is True
    with sqlite3.connect(db) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT status FROM agent_tasks WHERE id=?",
                        (tid,)).fetchone()
    assert row["status"] == "done"
