"""Phase 3c — dashboard /api/autofixes/<id>/{merge,reject} endpoint tests."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT_TESTS = Path(__file__).resolve().parents[1]
if str(REPO_ROOT_TESTS) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_TESTS))


_AUTO_FIXES_DDL = """
CREATE TABLE IF NOT EXISTS auto_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    symptom_category TEXT NOT NULL,
    symptom_hash TEXT NOT NULL,
    symptom_summary TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    prompt TEXT,
    diff_summary TEXT,
    files_changed_json TEXT,
    test_result TEXT,
    test_output_tail TEXT,
    pushed INTEGER DEFAULT 0,
    operator_decision TEXT,
    operator_decision_at_utc TEXT
)
"""


def _run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)}\n{p.stderr}\n{p.stdout}")
    return p


def _seed_autofix(db: Path, *, branch: str, pushed: int = 1, decision=None) -> int:
    with sqlite3.connect(str(db)) as conn:
        conn.execute(_AUTO_FIXES_DDL)
        cur = conn.execute(
            """
            INSERT INTO auto_fixes
                (started_at_utc, symptom_category, symptom_hash, symptom_summary,
                 branch_name, worktree_path, pushed, operator_decision)
            VALUES (?, 'cat', 'h', 'summary', ?, '/tmp/x', ?, ?)
            """,
            ("2026-05-09T10:00:00Z", branch, pushed, decision),
        )
        conn.commit()
        return int(cur.lastrowid)


def _setup_repo_with_branch(tmp_path: Path, *, branch: str, conflict: bool = False):
    """Create local repo + bare remote with master and an autofix branch.

    If ``conflict`` is True, the autofix branch and master both modify the
    same line of the same file, so a merge would conflict.
    """
    bare = tmp_path / "remote.git"
    _run(["git", "init", "-q", "--bare", "-b", "master", str(bare)])

    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "master", str(repo)])
    _run(["git", "-C", str(repo), "config", "user.email", "t@t"])
    _run(["git", "-C", str(repo), "config", "user.name", "T"])
    _run(["git", "-C", str(repo), "config", "commit.gpgsign", "false"])
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("orig\n")
    _run(["git", "-C", str(repo), "add", "."])
    _run(["git", "-C", str(repo), "commit", "-qm", "seed"])
    _run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)])
    _run(["git", "-C", str(repo), "push", "-q", "origin", "master"])

    # autofix branch with one new commit.
    _run(["git", "-C", str(repo), "checkout", "-qb", branch])
    if conflict:
        (repo / "src" / "x.py").write_text("autofix-version\n")
    else:
        (repo / "src" / "y.py").write_text("new feature\n")
    _run(["git", "-C", str(repo), "add", "."])
    _run(["git", "-C", str(repo), "commit", "-qm", "autofix: patch"])
    _run(["git", "-C", str(repo), "push", "-q", "origin", branch])

    # Back to master.
    _run(["git", "-C", str(repo), "checkout", "-q", "master"])

    if conflict:
        # Make a divergent commit on master that conflicts with the branch.
        (repo / "src" / "x.py").write_text("master-version\n")
        _run(["git", "-C", str(repo), "add", "."])
        _run(["git", "-C", str(repo), "commit", "-qm", "master change"])
        _run(["git", "-C", str(repo), "push", "-q", "origin", "master"])

    return repo, bare


@pytest.fixture()
def app_with_repo(tmp_path, monkeypatch):
    """Build a Flask app + a tmp git repo wired in as REPO_ROOT."""
    db_path = tmp_path / "judas.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db_path))

    from src.db.models import init_db
    init_db(db_path)

    from src.dashboard import app as dashboard_app
    monkeypatch.setattr(dashboard_app, "_db_path", lambda: db_path)

    def _factory(*, branch: str = "autofix/test-1", conflict: bool = False):
        repo, bare = _setup_repo_with_branch(tmp_path, branch=branch, conflict=conflict)
        monkeypatch.setattr(dashboard_app, "REPO_ROOT", repo)
        flask_app = dashboard_app.create_app()
        flask_app.testing = True
        return flask_app.test_client(), db_path, repo, bare

    return _factory


# ---------------------------------------------------------------------------
# Merge endpoint
# ---------------------------------------------------------------------------


def test_merge_endpoint_succeeds_on_clean_branch(app_with_repo):
    client, db, repo, bare = app_with_repo(branch="autofix/clean-1")
    autofix_id = _seed_autofix(db, branch="autofix/clean-1", pushed=1)

    pre = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    resp = client.post(f"/api/autofixes/{autofix_id}/merge")
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is True
    assert body["master_sha"] != pre  # advanced

    # Bare remote master matches local master.
    remote_sha = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_sha == body["master_sha"]

    # DB row updated.
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT operator_decision, operator_decision_at_utc FROM auto_fixes WHERE id=?",
            (autofix_id,),
        ).fetchone()
    assert row[0] == "merged"
    assert row[1] is not None


def test_merge_endpoint_rejects_unpushed(app_with_repo):
    client, db, repo, bare = app_with_repo(branch="autofix/unpushed-1")
    autofix_id = _seed_autofix(db, branch="autofix/unpushed-1", pushed=0)
    resp = client.post(f"/api/autofixes/{autofix_id}/merge")
    assert resp.status_code == 400
    assert "not pushed" in resp.get_json()["error"]


def test_merge_endpoint_rejects_already_decided(app_with_repo):
    client, db, repo, bare = app_with_repo(branch="autofix/decided-1")
    autofix_id = _seed_autofix(db, branch="autofix/decided-1", pushed=1, decision="rejected")
    resp = client.post(f"/api/autofixes/{autofix_id}/merge")
    assert resp.status_code == 400
    assert "already decided" in resp.get_json()["error"]


def test_merge_conflict_aborts_safely(app_with_repo):
    client, db, repo, bare = app_with_repo(branch="autofix/conflict-1", conflict=True)
    autofix_id = _seed_autofix(db, branch="autofix/conflict-1", pushed=1)

    pre_local = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    pre_remote = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    resp = client.post(f"/api/autofixes/{autofix_id}/merge")
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "merge failed" in body["error"]

    post_local = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    post_remote = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "master"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert post_local == pre_local, "local master must be untouched"
    assert post_remote == pre_remote, "remote master must be untouched"

    # No --MERGING-- state lingering.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "-b"],
        capture_output=True, text=True,
    ).stdout
    assert "MERGING" not in status

    # DB row not flipped.
    with sqlite3.connect(str(db)) as conn:
        decision = conn.execute(
            "SELECT operator_decision FROM auto_fixes WHERE id=?", (autofix_id,)
        ).fetchone()[0]
    assert decision is None


# ---------------------------------------------------------------------------
# Reject endpoint
# ---------------------------------------------------------------------------


def test_reject_endpoint_marks_decision(app_with_repo):
    client, db, repo, bare = app_with_repo(branch="autofix/reject-1")
    autofix_id = _seed_autofix(db, branch="autofix/reject-1", pushed=1)

    resp = client.post(f"/api/autofixes/{autofix_id}/reject", json={})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"ok": True}

    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT operator_decision, operator_decision_at_utc FROM auto_fixes WHERE id=?",
            (autofix_id,),
        ).fetchone()
    assert row[0] == "rejected"
    assert row[1] is not None

    # Idempotent / already-decided returns 400.
    resp2 = client.post(f"/api/autofixes/{autofix_id}/reject", json={})
    assert resp2.status_code == 400
