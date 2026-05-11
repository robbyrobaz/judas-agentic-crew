"""Tests for src.research.autofix_executor — Phase 3c."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.research import autofix_executor as ax  # noqa: E402


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)}\n{p.stderr}\n{p.stdout}")
    return p


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "master", str(path)])
    _run(["git", "-C", str(path), "config", "user.email", "test@example.com"])
    _run(["git", "-C", str(path), "config", "user.name", "Test"])
    _run(["git", "-C", str(path), "config", "commit.gpgsign", "false"])
    (path / "README.md").write_text("seed\n")
    _run(["git", "-C", str(path), "add", "README.md"])
    _run(["git", "-C", str(path), "commit", "-q", "-m", "seed"])
    return path


# ---------------------------------------------------------------------------
# can_autofix gates
# ---------------------------------------------------------------------------


def test_can_autofix_blocked_by_disable_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "_market_closed_or_weekend", lambda: True)
    monkeypatch.setattr(ax, "_db_path", lambda: None)
    (tmp_path / "autofix.disable").write_text("halt")
    ok, reason = ax.can_autofix(repo_root=tmp_path)
    assert ok is False
    assert "disable" in reason.lower()


def test_can_autofix_blocked_by_open_positions(tmp_path, monkeypatch):
    db = tmp_path / "judas_crew.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, status TEXT, "
            "symbol TEXT, direction TEXT, qty INTEGER, opened_at TEXT)"
        )
        conn.execute(_AUTO_FIXES_DDL)
        conn.execute(
            "INSERT INTO trades (status, symbol, direction, qty, opened_at) "
            "VALUES ('open', 'MGC', 'long', 1, '2026-05-09T12:00:00Z')"
        )
        conn.commit()
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setattr(ax, "_market_closed_or_weekend", lambda: True)
    ok, reason = ax.can_autofix(repo_root=tmp_path)
    assert ok is False
    assert "open positions" in reason


def test_can_autofix_blocked_by_recent_count(tmp_path, monkeypatch):
    db = tmp_path / "judas_crew.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, status TEXT, "
            "symbol TEXT, direction TEXT, qty INTEGER, opened_at TEXT)"
        )
        conn.execute(_AUTO_FIXES_DDL)
        # 3 rows in the last 24h.
        for i in range(3):
            conn.execute(
                "INSERT INTO auto_fixes (started_at_utc, symptom_category, symptom_hash, "
                "symptom_summary, branch_name, worktree_path) VALUES (?, ?, ?, ?, ?, ?)",
                ("2026-05-09T01:00:00Z", "test", f"h{i}", "s", f"autofix/x{i}", "/tmp/x"),
            )
        conn.commit()
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setattr(ax, "_market_closed_or_weekend", lambda: True)
    # patch _now_utc to be just past the seeded rows so they are all "in 24h".
    from datetime import datetime, timezone
    monkeypatch.setattr(
        ax, "_now_utc", lambda: datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    )
    # Daily cap is opt-in (set MAX_AUTOFIXES_PER_DAY > 0). When enabled,
    # `can_autofix` blocks once the recent count meets/exceeds it.
    monkeypatch.setattr(ax, "MAX_AUTOFIXES_PER_DAY", 3)
    ok, reason = ax.can_autofix(repo_root=tmp_path)
    assert ok is False
    assert "budget" in reason.lower()

    # With the cap disabled (default), three recent rows do NOT block.
    monkeypatch.setattr(ax, "MAX_AUTOFIXES_PER_DAY", 0)
    ok, reason = ax.can_autofix(repo_root=tmp_path)
    assert ok is True
    assert reason == "ok"


# ---------------------------------------------------------------------------
# create_autofix_worktree
# ---------------------------------------------------------------------------


def test_create_worktree_and_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    ctx = ax.create_autofix_worktree(
        autofix_id=42, symptom_slug="silent dry-run", repo_root=str(repo)
    )
    try:
        assert Path(ctx.worktree_path).exists()
        assert ctx.branch_name.startswith("autofix/")
        assert "silent-dry-run" in ctx.branch_name
        # Branch is checked out in the worktree.
        head = _run(
            ["git", "-C", ctx.worktree_path, "rev-parse", "--abbrev-ref", "HEAD"]
        ).stdout.strip()
        assert head == ctx.branch_name
    finally:
        ax.cleanup_worktree(worktree_path=ctx.worktree_path, branch_name=ctx.branch_name)


# ---------------------------------------------------------------------------
# Hook
# ---------------------------------------------------------------------------


def test_install_denylist_hook_executes(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    ctx = ax.create_autofix_worktree(
        autofix_id=1, symptom_slug="hook-test", repo_root=str(repo)
    )
    try:
        ax.install_denylist_hook(worktree_path=ctx.worktree_path)
        # Touch a deny-listed file.
        cfg = Path(ctx.worktree_path) / "config.yaml"
        cfg.write_text("evil: true\n")
        _run(["git", "-C", ctx.worktree_path, "add", "config.yaml"])
        proc = subprocess.run(
            ["git", "-C", ctx.worktree_path, "commit", "-m", "naughty"],
            capture_output=True, text=True,
        )
        # Commit object gets created, then post-commit hook rejects it.
        combined = proc.stdout + proc.stderr
        assert "DENIED" in combined, combined
        # And the hook rolled the commit back via reset --soft.
        log = _run(["git", "-C", ctx.worktree_path, "log", "--oneline"]).stdout
        assert log.count("\n") == 1  # only the seed commit
    finally:
        ax.cleanup_worktree(worktree_path=ctx.worktree_path, branch_name=ctx.branch_name)


def test_install_denylist_hook_allows_allowlisted(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    # Pre-create the allow-listed dir on master so the worktree branches from it.
    (Path(repo) / "src" / "research").mkdir(parents=True)
    (Path(repo) / "src" / "research" / "x.py").write_text("# orig\n")
    _run(["git", "-C", str(repo), "add", "src/research/x.py"])
    _run(["git", "-C", str(repo), "commit", "-q", "-m", "add research"])

    ctx = ax.create_autofix_worktree(
        autofix_id=2, symptom_slug="ok-test", repo_root=str(repo)
    )
    try:
        ax.install_denylist_hook(worktree_path=ctx.worktree_path)
        target = Path(ctx.worktree_path) / "src" / "research" / "x.py"
        target.write_text("# patched\n")
        _run(["git", "-C", ctx.worktree_path, "add", "src/research/x.py"])
        proc = subprocess.run(
            ["git", "-C", ctx.worktree_path, "commit", "-m", "ok"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "DENIED" not in (proc.stdout + proc.stderr)
        log = _run(["git", "-C", ctx.worktree_path, "log", "--oneline"]).stdout
        assert log.count("\n") == 3  # seed + add-research + new
    finally:
        ax.cleanup_worktree(worktree_path=ctx.worktree_path, branch_name=ctx.branch_name)


# ---------------------------------------------------------------------------
# commit_and_push
# ---------------------------------------------------------------------------


def test_commit_and_push_dryrun(tmp_path):
    bare = tmp_path / "remote.git"
    _run(["git", "init", "-q", "--bare", "-b", "master", str(bare)])
    repo = _init_repo(tmp_path / "repo")
    _run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)])
    _run(["git", "-C", str(repo), "push", "-q", "origin", "master"])

    ctx = ax.create_autofix_worktree(
        autofix_id=7, symptom_slug="push-test", repo_root=str(repo)
    )
    try:
        ax.install_denylist_hook(worktree_path=ctx.worktree_path)
        # Make an allow-listed change.
        d = Path(ctx.worktree_path) / "src" / "research"
        d.mkdir(parents=True, exist_ok=True)
        (d / "patch.py").write_text("# fix\n")
        result = ax.commit_and_push(
            worktree_path=ctx.worktree_path,
            branch_name=ctx.branch_name,
            message="autofix: patch",
        )
        assert result["ok"] is True, result
        assert result["pushed"] is True
        assert any("patch.py" in f for f in result["files_changed"])
        # Verify the bare remote has the branch.
        ls = _run(["git", "-C", str(bare), "branch", "--list"]).stdout
        assert ctx.branch_name in ls
    finally:
        ax.cleanup_worktree(worktree_path=ctx.worktree_path, branch_name=ctx.branch_name)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_cleanup_worktree_removes_dir_keeps_branch(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    ctx = ax.create_autofix_worktree(
        autofix_id=99, symptom_slug="cleanup", repo_root=str(repo)
    )
    assert Path(ctx.worktree_path).exists()
    ax.cleanup_worktree(
        worktree_path=ctx.worktree_path, branch_name=ctx.branch_name, keep_branch=True
    )
    assert not Path(ctx.worktree_path).exists()
    # Branch still exists.
    branches = _run(["git", "-C", str(repo), "branch", "--list"]).stdout
    assert ctx.branch_name in branches
