"""Synchronous autofix dispatch — shared by OperatorFlow and the
``delegate_to_coder`` tool.

The Coder has no systemd timer (by design — per AGENTIC_OPERATOR_PLAN.md).
The Operator delegates symptoms via ``delegate_to_coder``, which calls
``dispatch_symptom`` here. That records the symptom on ``auto_fixes`` and
runs the M2.7 autofix harness inline (worktree → patch → pytest →
commit + push), returning a structured result the Operator can read on
the next tool turn.

The same code path is reused by ``OperatorFlow._try_run_one_autofix`` —
when symptom-detection finds bugs and dispatches one fix at the end of
``fix_bug_step``, it calls ``run_one_autofix`` here.

Inhibit via ``JUDAS_AUTOFIX_INHIBIT=1`` (tests set this).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.research.symptoms import sha1_symptom

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_autofix(*, db_path: str, autofix_id: int, **fields: Any) -> None:
    """Patch arbitrary auto_fixes columns by id. `finished=True` sets
    finished_at_utc to now."""
    finished = fields.pop("finished", False)
    if finished:
        fields["finished_at_utc"] = _now_utc()
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [autofix_id]
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(f"UPDATE auto_fixes SET {cols} WHERE id = ?", vals)
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("autofix_dispatch.update_failed")


def insert_symptom_row(
    *, db_path: str, category: str, summary: str, evidence: str = "",
) -> int | None:
    """Insert an auto_fixes 'detected' row. Returns the new id, or the
    existing id if the symptom_hash collides with an already-open row
    (the unique partial index enforces this)."""
    h = sha1_symptom(category, evidence or summary)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                """
                INSERT INTO auto_fixes (
                    started_at_utc, symptom_category, symptom_hash,
                    symptom_summary, status
                ) VALUES (?, ?, ?, ?, 'detected')
                """,
                (_now_utc(), category, h, summary),
            )
            new_id = cur.lastrowid
            conn.commit()
            return int(new_id) if new_id else None
        except sqlite3.IntegrityError:
            # Already an open row for this hash — return its id so the
            # caller can target it.
            row = conn.execute(
                "SELECT id FROM auto_fixes WHERE symptom_hash = ? "
                "AND operator_decision IS NULL ORDER BY id DESC LIMIT 1",
                (h,),
            ).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("autofix_dispatch.insert_failed")
        return None


def run_one_autofix(
    *, db_path: str, target_autofix_id: int | None = None,
) -> dict:
    """Run the autofix harness on one 'detected' auto_fixes row.

    If ``target_autofix_id`` is given, fixes that specific row (used by
    ``delegate_to_coder`` so it can target the symptom it just inserted).
    Otherwise picks the oldest 'detected' row.

    Returns a structured result dict. Never raises — errors are logged
    and reflected in the result.
    """
    if os.environ.get("JUDAS_AUTOFIX_INHIBIT") == "1":
        log.info("autofix_dispatch.inhibited_by_env")
        return {"ok": False, "status": "inhibited",
                "reason": "JUDAS_AUTOFIX_INHIBIT=1"}

    from src.research.autofix_executor import (
        ALLOWLIST_PATTERNS, DENYLIST_PATTERNS, can_autofix,
        cleanup_worktree, commit_and_push, create_autofix_worktree,
        install_denylist_hook,
    )
    from src.research.autofix_harness import run_harness

    allowed, reason = can_autofix()
    if not allowed:
        log.info("autofix_dispatch.gated", extra={"reason": reason})
        return {"ok": False, "status": "gated", "reason": reason}

    repo_root = str(_REPO_ROOT)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        if target_autofix_id is not None:
            row = conn.execute(
                """SELECT id, symptom_category, symptom_hash, symptom_summary
                   FROM auto_fixes WHERE id = ? AND status = 'detected'
                   AND operator_decision IS NULL""",
                (int(target_autofix_id),),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT id, symptom_category, symptom_hash, symptom_summary
                   FROM auto_fixes
                   WHERE status = 'detected' AND operator_decision IS NULL
                   ORDER BY started_at_utc DESC LIMIT 1"""
            ).fetchone()
        conn.close()
    except sqlite3.Error:
        log.exception("autofix_dispatch.select_failed")
        return {"ok": False, "status": "error", "reason": "select_failed"}
    if row is None:
        log.info("autofix_dispatch.no_detected_rows")
        return {"ok": False, "status": "no_target",
                "reason": "no detected auto_fixes row"}

    autofix_id = int(row["id"])
    symptom_category = row["symptom_category"]
    symptom_summary = row["symptom_summary"]
    slug = (symptom_category or "fix").replace(" ", "-")[:32]

    try:
        ctx = create_autofix_worktree(
            autofix_id=autofix_id, symptom_slug=slug, repo_root=repo_root,
        )
        install_denylist_hook(worktree_path=ctx.worktree_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("autofix_dispatch.worktree_failed")
        update_autofix(
            db_path=db_path, autofix_id=autofix_id, status="error",
            test_result=None, diff_summary=None, files_changed_json=None,
            prompt=None, branch_name=None, worktree_path=None, pushed=0,
            finished=True,
            test_output_tail=f"worktree creation failed: {exc}",
        )
        return {"ok": False, "status": "error",
                "autofix_id": autofix_id, "reason": f"worktree: {exc}"}

    prompt = (
        f"Symptom category: {symptom_category}\n"
        f"Summary: {symptom_summary}\n\n"
        f"Diagnose the root cause within the allowlisted files only. "
        f"Apply the minimal patch that resolves the symptom and makes "
        f"pytest pass. If the fix requires touching a denylisted file, "
        f"explain why and stop.\n"
    )

    update_autofix(
        db_path=db_path, autofix_id=autofix_id, status="running",
        branch_name=ctx.branch_name, worktree_path=ctx.worktree_path,
        prompt=prompt,
    )

    try:
        result = run_harness(
            prompt=prompt, worktree_path=ctx.worktree_path,
            allowlist=list(ALLOWLIST_PATTERNS),
            denylist=list(DENYLIST_PATTERNS),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("autofix_dispatch.harness_failed")
        cleanup_worktree(worktree_path=ctx.worktree_path,
                         branch_name=ctx.branch_name, keep_branch=True)
        update_autofix(
            db_path=db_path, autofix_id=autofix_id, status="error",
            test_result="error",
            test_output_tail=f"harness exception: {exc}", finished=True,
        )
        return {"ok": False, "status": "error",
                "autofix_id": autofix_id, "reason": f"harness: {exc}"}

    try:
        files_changed_json = json.dumps(result.files_changed)
    except Exception:  # noqa: BLE001
        files_changed_json = "[]"

    if not result.success:
        log.warning("autofix_dispatch.harness_no_fix",
                    extra={"error": result.error,
                           "test_passed": result.test_passed})
        cleanup_worktree(worktree_path=ctx.worktree_path,
                         branch_name=ctx.branch_name, keep_branch=False)
        update_autofix(
            db_path=db_path, autofix_id=autofix_id, status="error",
            test_result="failed" if not result.test_passed else "skipped",
            diff_summary=result.diff_summary,
            files_changed_json=files_changed_json,
            test_output_tail=(result.test_output_tail
                              or result.error or "")[:4000],
            finished=True,
        )
        return {"ok": False, "status": "harness_no_fix",
                "autofix_id": autofix_id,
                "test_passed": bool(result.test_passed),
                "error": (result.error or "")[:400],
                "diff_summary": result.diff_summary}

    commit_message = f"autofix({symptom_category}): {symptom_summary[:60]}"
    push_result = commit_and_push(
        worktree_path=ctx.worktree_path, branch_name=ctx.branch_name,
        message=commit_message,
    )
    cleanup_worktree(worktree_path=ctx.worktree_path,
                     branch_name=ctx.branch_name, keep_branch=True)

    if push_result.get("denied"):
        log.warning("autofix_dispatch.denylist_hook_rejected",
                    extra=push_result)
        update_autofix(
            db_path=db_path, autofix_id=autofix_id, status="denied",
            test_result="denied", diff_summary=result.diff_summary,
            files_changed_json=files_changed_json,
            test_output_tail=(push_result.get("error") or "denylist hit")[:4000],
            pushed=0, finished=True,
        )
        return {"ok": False, "status": "denied",
                "autofix_id": autofix_id,
                "reason": push_result.get("error") or "denylist hit"}

    update_autofix(
        db_path=db_path, autofix_id=autofix_id, status="completed",
        test_result="passed" if result.test_passed else "failed",
        diff_summary=result.diff_summary,
        files_changed_json=files_changed_json,
        test_output_tail=(result.test_output_tail or "")[:4000],
        pushed=1 if push_result.get("pushed") else 0, finished=True,
    )
    log.warning("autofix_dispatch.completed",
                extra={"autofix_id": autofix_id,
                       "branch": ctx.branch_name,
                       "pushed": push_result.get("pushed")})
    return {"ok": True, "status": "completed",
            "autofix_id": autofix_id,
            "branch": ctx.branch_name,
            "pushed": bool(push_result.get("pushed")),
            "test_passed": bool(result.test_passed),
            "diff_summary": result.diff_summary,
            "files_changed": result.files_changed}


def dispatch_symptom(
    *, db_path: str, symptom: str, context: str = "",
    category: str = "tool_failure",
) -> dict:
    """Operator's ``delegate_to_coder`` entrypoint.

    Records the symptom on ``auto_fixes`` (or reuses an existing open
    row), then runs the harness synchronously and returns the result.
    """
    if os.environ.get("JUDAS_AUTOFIX_INHIBIT") == "1":
        log.info("autofix_dispatch.delegate.inhibited")
        return {"ok": False, "status": "inhibited",
                "reason": "JUDAS_AUTOFIX_INHIBIT=1"}
    summary = (symptom or "").strip()[:300] or "unspecified symptom"
    evidence = f"{summary}\n{(context or '').strip()[:1500]}"
    autofix_id = insert_symptom_row(
        db_path=db_path, category=category,
        summary=summary, evidence=evidence,
    )
    if autofix_id is None:
        return {"ok": False, "status": "insert_failed",
                "reason": "could not insert/find auto_fixes row"}
    log.info("autofix_dispatch.delegate", extra={"autofix_id": autofix_id,
                                                 "category": category})
    result = run_one_autofix(db_path=db_path, target_autofix_id=autofix_id)
    _publish_coder_report(
        db_path=db_path, autofix_id=autofix_id, symptom=summary,
        context=(context or "").strip(), result=result,
    )
    return result


# Local log file the coder writes its narrative to. Anyone (human or
# agent) can tail it; the same content also lands in the findings table
# so other agents see it during read_findings().
_CODER_LOG = _REPO_ROOT / "logs" / "coder.log"


def _publish_coder_report(*, db_path: str, autofix_id: int,
                          symptom: str, context: str, result: dict) -> None:
    """Write a coder-cycle report to logs/coder.log AND to the findings
    table (author='coder') so the rest of the team can read what the
    coder attempted, succeeded at, or failed on."""
    status = str(result.get("status") or "unknown")
    ok = bool(result.get("ok"))
    branch = result.get("branch")
    pushed = result.get("pushed")
    test_passed = result.get("test_passed")
    diff_summary = result.get("diff_summary")
    files_changed = result.get("files_changed") or []
    reason = result.get("reason") or result.get("error") or ""

    title = (
        f"Coder autofix #{autofix_id} — "
        f"{('passed' if ok else 'failed')}: {symptom[:80]}"
    )
    body_lines = [
        f"autofix_id: {autofix_id}",
        f"status: {status}",
        f"branch: {branch or '(none)'}",
        f"pushed: {pushed}",
        f"test_passed: {test_passed}",
        f"files_changed: {files_changed}",
        f"diff_summary: {(diff_summary or '')[:600]}",
        f"reason: {reason[:600]}" if reason else "",
        "",
        f"symptom: {symptom}",
        f"context: {context[:1200]}" if context else "",
    ]
    body = "\n".join(line for line in body_lines if line is not None)

    # 1. Append to the shared log file.
    try:
        _CODER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _CODER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n===== {_now_utc()} =====\n")
            fh.write(title + "\n")
            fh.write(body + "\n")
    except OSError:
        log.exception("autofix_dispatch.coder_log_write_failed")

    # 2. Record a finding so other agents see it in read_findings().
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                INSERT INTO findings (
                    created_at_utc, author, title, body, refs_json
                ) VALUES (?, 'coder', ?, ?, ?)
                """,
                (_now_utc(), title[:200], body,
                 json.dumps({"autofix_id": autofix_id,
                             "branch": branch,
                             "pushed": pushed,
                             "test_passed": test_passed,
                             "status": status})),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("autofix_dispatch.coder_finding_write_failed")
