"""Phase 3a — auto_fixes queue endpoints + fix_bug_step + routing tests."""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db


def _insert_autofix(
    db_path: str,
    *,
    category: str,
    sym_hash: str,
    summary: str,
    started_at: str = "2026-05-09T10:00:00Z",
    operator_decision: str | None = None,
    status: str = "detected",
) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO auto_fixes
                (started_at_utc, symptom_category, symptom_hash, symptom_summary,
                 operator_decision, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (started_at, category, sym_hash, summary, operator_decision, status),
        )
        conn.commit()
        return int(cur.lastrowid)


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "judas.db"
    init_db(db_path)
    monkeypatch.setenv("JUDAS_DB_PATH", str(db_path))
    from src.dashboard import app as dashboard_app

    monkeypatch.setattr(dashboard_app, "_db_path", lambda: db_path)
    flask_app = dashboard_app.create_app()
    flask_app.testing = True
    with flask_app.test_client() as client:
        yield client, str(db_path)


def test_get_autofixes_returns_open_only_by_default(app_client):
    client, db_path = app_client
    open_id = _insert_autofix(
        db_path, category="tool_failure", sym_hash="h_open", summary="open one"
    )
    closed_id = _insert_autofix(
        db_path,
        category="silent_dryrun",
        sym_hash="h_closed",
        summary="closed one",
        operator_decision="rejected",
        started_at="2026-05-08T10:00:00Z",
    )

    resp = client.get("/api/autofixes")
    assert resp.status_code == 200
    body = resp.get_json()
    ids = [row["id"] for row in body["autofixes"]]
    assert open_id in ids
    assert closed_id not in ids

    resp_all = client.get("/api/autofixes?status=all")
    body_all = resp_all.get_json()
    ids_all = [row["id"] for row in body_all["autofixes"]]
    assert open_id in ids_all
    assert closed_id in ids_all


def test_get_autofix_detail_and_404(app_client):
    client, db_path = app_client
    aid = _insert_autofix(
        db_path, category="tool_failure", sym_hash="h_x", summary="detail row"
    )
    # Add some detail fields.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE auto_fixes SET prompt=?, diff_summary=?, files_changed_json=? WHERE id=?",
            ("the prompt", "1 file changed", '["src/foo.py"]', aid),
        )
        conn.commit()

    resp = client.get(f"/api/autofixes/{aid}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["id"] == aid
    assert body["prompt"] == "the prompt"
    assert body["files_changed"] == ["src/foo.py"]

    resp_404 = client.get("/api/autofixes/99999")
    assert resp_404.status_code == 404


# --- fix_bug_step + routing -------------------------------------------------


def _reload_operator_flow(monkeypatch, tmp_path) -> tuple:
    state_db = tmp_path / "flow_state.db"
    judas_db = tmp_path / "judas_crew.db"
    init_db(judas_db)
    monkeypatch.setenv("JUDAS_OPERATOR_STATE_DB", str(state_db))
    monkeypatch.setenv("JUDAS_DB_PATH", str(judas_db))
    for name in list(sys.modules):
        if name == "src.flows.operator_flow" or name.startswith("src.flows.operator_flow."):
            del sys.modules[name]
    module = importlib.import_module("src.flows.operator_flow")
    return module, str(judas_db)


def test_fix_bug_step_inserts_row(tmp_path, monkeypatch):
    module, judas_db = _reload_operator_flow(monkeypatch, tmp_path)

    flow = module.OperatorFlow()
    flow.state.findings = {
        "pending_symptoms": [
            {
                "category": "tool_failure",
                "hash": "abc123",
                "summary": "boom 3x in 24h",
                "evidence": {"signature": "boom", "count": 3},
            }
        ]
    }
    flow.fix_bug_step()

    with sqlite3.connect(judas_db) as conn:
        rows = conn.execute(
            "SELECT symptom_category, symptom_hash, symptom_summary, status FROM auto_fixes"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0] == ("tool_failure", "abc123", "boom 3x in 24h", "detected")


def test_fix_bug_step_skips_duplicate_open_symptom(tmp_path, monkeypatch):
    module, judas_db = _reload_operator_flow(monkeypatch, tmp_path)

    # Pre-insert an open row with the same hash.
    _insert_autofix(
        judas_db, category="tool_failure", sym_hash="dupehash", summary="prior"
    )

    flow = module.OperatorFlow()
    flow.state.findings = {
        "pending_symptoms": [
            {
                "category": "tool_failure",
                "hash": "dupehash",
                "summary": "second attempt",
                "evidence": {},
            }
        ]
    }
    flow.fix_bug_step()

    with sqlite3.connect(judas_db) as conn:
        rows = conn.execute(
            "SELECT symptom_summary FROM auto_fixes WHERE symptom_hash='dupehash'"
        ).fetchall()
    # Still only one row for this hash.
    assert len(rows) == 1
    assert rows[0][0] == "prior"


def test_morning_review_routes_to_fix_bug_when_symptoms(tmp_path, monkeypatch):
    crewai = pytest.importorskip("crewai")
    if tuple(int(p) for p in crewai.__version__.split(".")[:2]) < (1, 8):
        pytest.skip("requires crewai >= 1.8")

    module, judas_db = _reload_operator_flow(monkeypatch, tmp_path)

    # Stub live review to return no retires.
    import src.research.live_review as live_review_mod

    monkeypatch.setattr(
        live_review_mod, "review_all_active_strategies", lambda *, db_path: []
    )

    # Stub symptom detection to return one symptom.
    from src.research import symptoms as symptoms_mod

    fake_sym = symptoms_mod.Symptom(
        category="silent_dryrun",
        hash="hash_abc",
        summary="silent dry-run hits",
        evidence={"count": 5},
    )
    monkeypatch.setattr(
        symptoms_mod,
        "detect_all_symptoms",
        lambda *, db_path, repo_root: [fake_sym],
    )

    flow = module.OperatorFlow()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})
    assert flow.state.decision == "fix_bug"
    assert flow.state.findings is not None
    pending = flow.state.findings.get("pending_symptoms")
    assert pending and pending[0]["hash"] == "hash_abc"

    # And a row landed in auto_fixes.
    with sqlite3.connect(judas_db) as conn:
        rows = conn.execute(
            "SELECT symptom_hash, status FROM auto_fixes"
        ).fetchall()
    assert ("hash_abc", "detected") in rows
