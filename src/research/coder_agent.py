"""Coder specialist (Phase 10).

Mandate: fix bugs via the existing Phase 3 autofix harness. Pulls
team='coder' agent_tasks rows; for each, records a symptom in
auto_fixes and triggers ``_try_run_one_autofix``. Does NOT touch
order-routing files (deny-list enforced by the existing post-commit
hook).
Always check the current time and market hours when reasoning about timing.

"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.research import agent_tools

log = logging.getLogger(__name__)


# The Coder runs a deterministic loop (no LLM ReAct), but still exposes
# the shared queue + findings tools. Listed here for palette regression
# tests and so any future LLM-driven coder cycle inherits them.
SYSTEM_PROMPT = """\
You are the Coder on an autonomous futures trading system that trades a
REAL Lucid 50K Flex EVAL account via NinjaTrader (see config.yaml; sim/paper
era ended 2026-07-26). ROB'S STANDING MANDATE (2026-08-16, THE 2-LOT ERA):
pass the $3,000 eval FAST. On an eval the only real cash at risk is the ~$95
reset fee — speed beats equity caution; a reset on variance is an accepted
cost. Micros (MNQ/MGC/MCL) trade a 2-lot floor (scan-enforced). The guards
(daily loss soft -$700 / hard -$900 flatten, $2k trailing MLL, 4-contract
cap, EOD flat) exist ONLY to stay under Lucid's unannounced ~$1,200 daily
loss limit — respect them absolutely, but never add caution beyond them.
FUNDED accounts are the opposite: real payout money, conservative gate
(PF >= 1.3 net over >= 100 trades) before sizing there.

You triage and fix
bugs reported by the Operator (and by symptom detection). The
delegate_to_coder tool path invokes the autofix harness inline; you
also have the queue (claim_task/complete_task/get_open_tasks) and the
team memory.

Only record a finding when you've learned something materially new
(e.g. a recurring bug pattern, a fix recipe). Don't write a finding
just because a cycle ended.
"""

INCLUDE_TOOLS = {    "claim_task", "complete_task", "get_open_tasks",
    # shared findings memory
    "record_finding", "read_findings", "retract_finding",
    "get_strategy_dossier",
    # Real hands — repo-confined file+shell (2026-06-29, per max-autonomy policy)
    "read_file", "list_files", "read_research_artifact",
    "write_file", "edit_file", "run_shell",
}


@dataclass
class CoderResult:
    success: bool
    actions_taken: list[dict]
    narrative: str
    turns_used: int
    elapsed_s: float
    fallback_used: bool
    raw_messages: list[dict] = field(default_factory=list)
    error: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_symptom(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _autofix_status(db_path: str, autofix_id: int | None) -> str:
    """Outcome of an autofix from its own record. 'completed' = a real, tested
    change landed; anything else means it did NOT actually fix the bug."""
    if autofix_id is None:
        return "no_autofix_id"
    from src.db.models import get_conn
    try:
        with get_conn(db_path) as conn:
            row = conn.execute("SELECT status FROM auto_fixes WHERE id = ?", (autofix_id,)).fetchone()
        return str(row[0]) if row else "missing"
    except Exception:  # noqa: BLE001
        return "unknown"


def _record_symptom(*, db_path: str, category: str, summary: str) -> int | None:
    from src.db.models import init_db
    init_db(db_path)
    h = _hash_symptom(f"{category}|{summary}")
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO auto_fixes (
                    started_at_utc, symptom_category, symptom_hash,
                    symptom_summary, status
                ) VALUES (?, ?, ?, ?, 'detected')
                """,
                (_utc_now(), category, h, summary),
            )
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # An open auto_fixes row already exists for this symptom.
            return None
        finally:
            conn.close()
    except sqlite3.Error:
        log.exception("coder_agent.record_symptom.failed")
        return None


def run_coder_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
) -> CoderResult:
    """Run one Coder cycle.

    The Coder doesn't run an LLM ReAct loop directly — it consumes
    queued ``team='coder'`` tasks, records each as a symptom, and
    invokes ``OperatorFlow._try_run_one_autofix`` once per cycle (the
    Phase 3 harness already enforces gates, deny-list, single-fix-per-
    cycle, and timeout).
    """
    started = time.time()
    if os.environ.get("JUDAS_CODER_AGENT_INHIBIT") == "1":
        log.info("coder_agent.inhibited_by_env")
        return CoderResult(
            success=True, actions_taken=[],
            narrative="Coder inhibited via JUDAS_CODER_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, _schemas = agent_tools.make_tools(
        db_path=db_path,
        include=INCLUDE_TOOLS,
        team="coder", claimed_by="coder_agent", author="coder",
    )

    actions: list[dict] = []
    open_tasks = tools["get_open_tasks"](limit=turn_budget)
    if isinstance(open_tasks, dict) and "error" in open_tasks:
        return CoderResult(
            success=False, actions_taken=[],
            narrative=f"get_open_tasks failed: {open_tasks.get('error')}",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=False, error=str(open_tasks.get("error")),
        )

    if not open_tasks:
        return CoderResult(
            success=True, actions_taken=[],
            narrative="No coder tasks queued.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=False,
        )

    fixed_one = False
    for task in open_tasks:
        tid = int(task["id"])
        # One autofix per cycle. DON'T claim+fake-"done" the rest — leave them
        # OPEN so a later cycle actually works them (the old code marked every
        # claimed task done regardless of whether any fix ran).
        if fixed_one:
            break
        claimed = tools["claim_task"](task_id=tid)
        if not claimed.get("ok"):
            actions.append({"task_id": tid, "ok": False,
                            "error": claimed.get("error")})
            continue
        payload = task.get("payload") or {}
        symptom = str(payload.get("symptom") or task.get("rationale") or "unknown")
        context = str(payload.get("context") or "")
        category = str(task.get("action") or "autofix_symptom")
        autofix_id = _record_symptom(db_path=db_path, category=category,
                                     summary=f"{symptom}\n{context}")
        result = {"autofix_id": autofix_id, "symptom": symptom}
        if autofix_id is not None:
            try:
                from src.flows.operator_flow import OperatorFlow
                flow = OperatorFlow.__new__(OperatorFlow)
                OperatorFlow._try_run_one_autofix(flow, db_path=db_path)
                fixed_one = True
                result["dispatched"] = True
            except Exception as exc:  # noqa: BLE001
                log.exception("coder_agent.dispatch_failed")
                result["dispatched"] = False
                result["dispatch_error"] = f"{type(exc).__name__}: {exc}"

        # VERIFY before marking done — never fake completion. The autofix records
        # its own outcome in auto_fixes.status; only 'completed' means a real,
        # tested code change actually landed. Anything else (error / empty diff /
        # not-run) is NOT done -> mark the task FAILED so it stays visible and
        # retryable instead of silently claiming success (the Jun-18 bug: the
        # autofix errored on both tasks but they were marked 'done' anyway).
        fix_status = _autofix_status(db_path, autofix_id)
        result["autofix_status"] = fix_status
        if fix_status == "completed":
            tools["complete_task"](task_id=tid, result=result, status="done")
        else:
            result["error"] = (f"autofix did not land (auto_fixes.status={fix_status}); "
                               f"task NOT marked done")
            tools["complete_task"](task_id=tid, result=result, status="failed")
        actions.append({"task_id": tid, **result})

    return CoderResult(
        success=True, actions_taken=actions,
        narrative=f"Processed {len(actions)} coder task(s); "
                  f"{'1 autofix dispatched' if fixed_one else 'no dispatch'}.",
        turns_used=len(actions), elapsed_s=time.time() - started,
        fallback_used=False,
    )
