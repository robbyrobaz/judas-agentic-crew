"""Auto-apply brief decisions and auto-merge successful autofix branches.

Removes the human-in-the-loop click-through that used to gate every
registry mutation and every autofix merge. The Operator team is now
peer-level — its proposals execute automatically.

Manual rails still available:
* `autofix.disable` file halts all autofix dispatch (so no new branches
  to merge).
* `kill.flag` file halts trading entirely.
* The order-path deny-list (broker / risk / config) is enforced by the
  post-commit hook regardless of who triggers the merge.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import strategy_registry
from src.db.conn import get_conn

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_brief_decisions_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brief_action_decisions (
            brief_date TEXT NOT NULL,
            action_index INTEGER NOT NULL,
            decision TEXT NOT NULL,
            action_json TEXT NOT NULL,
            result TEXT NOT NULL,
            decided_at_utc TEXT NOT NULL,
            PRIMARY KEY (brief_date, action_index)
        )
        """
    )


def auto_apply_brief_actions(
    *, db_path: str, brief_date: str, actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply every action in a freshly-persisted brief automatically.

    For each entry in ``actions``:
      * retire → calls ``strategy_registry.retire_strategy``.
      * review → marked acknowledged (no DB mutation).
      * any other type → recorded with a 'skipped: unknown type' note.

    Each decision is recorded in ``brief_action_decisions`` with
    decision='auto_applied' so the dashboard renders them as already
    decided. Returns the list of results for logging.
    """
    if os.environ.get("JUDAS_AUTOAPPLY_INHIBIT") == "1":
        log.info("auto_apply.brief.inhibited_by_env")
        return []

    results: list[dict[str, Any]] = []
    for idx, action in enumerate(actions or []):
        result_text = _apply_one_brief_action(action)
        try:
            with get_conn(db_path) as conn:
                _ensure_brief_decisions_table(conn)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO brief_action_decisions
                        (brief_date, action_index, decision, action_json,
                         result, decided_at_utc)
                    VALUES (?, ?, 'auto_applied', ?, ?, ?)
                    """,
                    (brief_date, idx, json.dumps(action),
                     result_text, _now_utc()),
                )
                conn.commit()
        except sqlite3.Error:
            log.exception("auto_apply.brief.persist_failed",
                          extra={"action_index": idx})
        results.append({"index": idx, "type": action.get("type"),
                        "result": result_text})
        log.info("auto_apply.brief.applied",
                 extra={"brief_date": brief_date, "index": idx,
                        "type": action.get("type"), "result": result_text})
    return results


def _apply_one_brief_action(action: dict[str, Any]) -> str:
    atype = str(action.get("type") or "").lower()
    if atype == "retire":
        demotion_id = action.get("demotion_id")
        sid = action.get("strategy_id")
        if demotion_id is not None:
            return f"already retired (demotion_id={demotion_id})"
        if sid is None:
            return "skipped: missing strategy_id"
        try:
            new_demotion = strategy_registry.retire_strategy(
                strategy_id=int(sid),
                reason=str(action.get("reason") or "auto-applied (brief)"),
                metrics_snapshot={"source": "daily_brief_autoapply"},
            )
            return f"retired strategy {sid}; demotion_id={new_demotion}"
        except Exception as exc:  # noqa: BLE001
            return f"retire failed: {exc}"
    if atype == "review":
        return "review acknowledged"
    return f"skipped: unknown action type '{atype}'"


def auto_merge_autofix(
    *, db_path: str, autofix_id: int, branch_name: str,
) -> dict[str, Any]:
    """Merge a successful autofix branch into master and push.

    Same safety pattern as the dashboard's manual merge endpoint:
      1. capture pre_sha
      2. local merge --no-ff; on fail, abort + reset, return error
      3. push origin master; on fail, hard reset to pre_sha, return error
      4. on success, update auto_fixes.operator_decision='merged'
    """
    if os.environ.get("JUDAS_AUTOAPPLY_INHIBIT") == "1":
        log.info("auto_apply.autofix.inhibited_by_env",
                 extra={"autofix_id": autofix_id})
        return {"ok": False, "reason": "inhibited"}

    repo = str(_REPO_ROOT)

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True,
        )

    pre = _git("rev-parse", "master")
    if pre.returncode != 0:
        return {"ok": False,
                "reason": f"resolve master: {pre.stderr.strip()}"}
    pre_sha = pre.stdout.strip()

    co = _git("checkout", "master")
    if co.returncode != 0:
        return {"ok": False,
                "reason": f"checkout master: {co.stderr.strip()}"}

    merge = _git("merge", "--no-ff", "-m",
                 f"Merge autofix branch {branch_name}", branch_name)
    if merge.returncode != 0:
        _git("merge", "--abort")
        _git("reset", "--hard", pre_sha)
        return {"ok": False,
                "reason": (merge.stderr.strip()
                           or merge.stdout.strip())[:400]}

    push = _git("push", "origin", "master")
    if push.returncode != 0:
        _git("reset", "--hard", pre_sha)
        return {"ok": False,
                "reason": f"push failed; rolled back: {push.stderr.strip()}"}

    post = _git("rev-parse", "master")
    master_sha = post.stdout.strip() if post.returncode == 0 else ""

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                UPDATE auto_fixes
                SET operator_decision = 'merged',
                    operator_decision_at_utc = ?
                WHERE id = ?
                """,
                (_now_utc(), autofix_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("auto_apply.autofix.row_update_failed")

    log.warning("auto_apply.autofix.merged",
                extra={"autofix_id": autofix_id, "branch": branch_name,
                       "master_sha": master_sha[:12]})
    return {"ok": True, "master_sha": master_sha, "branch": branch_name}
