"""Phase 4 dashboard briefs endpoint tests.

Worker E owns the daily_briefs table migration, but for this test suite we
create the table manually so the dashboard endpoints can be exercised in
isolation.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_DAILY_BRIEFS_DDL = """
CREATE TABLE IF NOT EXISTS daily_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    content_md TEXT,
    summary_json TEXT,
    acknowledged_at_utc TEXT
)
"""


def _seed_brief(db_path: str, brief_date: str, *, content_md: str, summary: dict) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute(_DAILY_BRIEFS_DDL)
        cur = conn.execute(
            """
            INSERT INTO daily_briefs (brief_date, created_at_utc, content_md, summary_json)
            VALUES (?, ?, ?, ?)
            """,
            (brief_date, f"{brief_date}T12:00:00Z", content_md, json.dumps(summary)),
        )
        conn.commit()
        return int(cur.lastrowid)


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "briefs.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db_path))

    from src.db.models import init_db
    init_db(db_path)

    # Seed two briefs.
    summary_1 = {
        "fires": 3,
        "fills": 2,
        "pnl_total": 42.5,
        "pnl_by_strategy": [{"strategy_id": 1, "name": "judas-mgc", "pnl": 42.5, "trades": 2}],
        "regime": {"vol_regime": "mid", "trend": "trending", "leaders": ["MGC"]},
        "surprises": [],
        "recommended_actions": [
            {"type": "retire", "strategy_id": 99, "reason": "PF below 0.9", "demotion_id": None},
            {"type": "review", "strategy_id": 1, "reason": "drawdown spiked"},
        ],
    }
    summary_2 = {
        "fires": 0,
        "fills": 0,
        "pnl_total": 0.0,
        "pnl_by_strategy": [],
        "regime": {"vol_regime": "low", "trend": "ranging", "leaders": []},
        "surprises": [],
        "recommended_actions": [],
    }
    _seed_brief(str(db_path), "2026-05-08", content_md="# Brief 2026-05-08\n\nDetails.", summary=summary_1)
    _seed_brief(str(db_path), "2026-05-07", content_md="# Brief 2026-05-07\n\nDetails.", summary=summary_2)

    from src.dashboard import app as dashboard_app

    # Override _db_path so the endpoints use our test DB without re-loading config.
    monkeypatch.setattr(dashboard_app, "_db_path", lambda: db_path)

    flask_app = dashboard_app.create_app()
    flask_app.testing = True
    with flask_app.test_client() as client:
        yield client, str(db_path)


def test_list_briefs_returns_recent(app_client):
    client, _ = app_client
    resp = client.get("/api/briefs")
    assert resp.status_code == 200
    body = resp.get_json()
    briefs = body["briefs"]
    assert len(briefs) == 2
    # Most recent first.
    assert briefs[0]["brief_date"] == "2026-05-08"
    assert briefs[1]["brief_date"] == "2026-05-07"
    assert briefs[0]["summary"]["pnl_total"] == 42.5
    assert briefs[0]["summary"]["fires"] == 3
    assert briefs[0]["acknowledged_at_utc"] is None


def test_get_brief_by_date_404_for_missing(app_client):
    client, _ = app_client
    resp = client.get("/api/briefs/2099-01-01")
    assert resp.status_code == 404


def test_get_brief_includes_markdown(app_client):
    client, _ = app_client
    resp = client.get("/api/briefs/2026-05-08")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["brief_date"] == "2026-05-08"
    assert "# Brief 2026-05-08" in body["content_md"]
    assert body["summary"]["fires"] == 3
    assert body["decisions"] == []


def test_ack_sets_timestamp(app_client):
    client, db_path = app_client
    resp = client.post("/api/briefs/2026-05-08/ack")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    with sqlite3.connect(db_path) as conn:
        first = conn.execute(
            "SELECT acknowledged_at_utc FROM daily_briefs WHERE brief_date='2026-05-08'"
        ).fetchone()
    assert first[0] is not None

    # Second ack is idempotent — timestamp unchanged.
    resp2 = client.post("/api/briefs/2026-05-08/ack")
    assert resp2.status_code == 200
    with sqlite3.connect(db_path) as conn:
        second = conn.execute(
            "SELECT acknowledged_at_utc FROM daily_briefs WHERE brief_date='2026-05-08'"
        ).fetchone()
    assert second[0] == first[0]

    # Missing brief returns 404.
    resp3 = client.post("/api/briefs/2099-01-01/ack")
    assert resp3.status_code == 404


def test_apply_retire_calls_registry(app_client):
    client, _ = app_client
    from src.dashboard import app as dashboard_app

    with patch.object(
        dashboard_app.strategy_registry, "retire_strategy", return_value=4242
    ) as mock_retire:
        resp = client.post("/api/briefs/2026-05-08/recommended/0/apply")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert "retired strategy 99" in body["result"]
    mock_retire.assert_called_once()
    kwargs = mock_retire.call_args.kwargs
    assert kwargs["strategy_id"] == 99
    assert "PF below 0.9" in kwargs["reason"]

    # Idempotent — second call does NOT invoke retire_strategy again.
    with patch.object(
        dashboard_app.strategy_registry, "retire_strategy", return_value=9999
    ) as mock_retire_2:
        resp2 = client.post("/api/briefs/2026-05-08/recommended/0/apply")
    assert resp2.status_code == 200
    assert resp2.get_json().get("already_decided") is True
    mock_retire_2.assert_not_called()


def test_reject_records_decision(app_client):
    client, db_path = app_client
    resp = client.post("/api/briefs/2026-05-08/recommended/1/reject")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["decision"] == "reject"

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT decision, result FROM brief_action_decisions
            WHERE brief_date='2026-05-08' AND action_index=1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "reject"

    # And the GET surfaces the decision.
    detail = client.get("/api/briefs/2026-05-08").get_json()
    assert any(d["action_index"] == 1 and d["decision"] == "reject" for d in detail["decisions"])
