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
You are the Coder on a paper futures trading lab. You triage and fix
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
        if not fixed_one and autofix_id is not None:
            try:
                from src.flows.operator_flow import OperatorFlow
                # Borrow the existing _try_run_one_autofix machinery.
                flow = OperatorFlow.__new__(OperatorFlow)
                # _try_run_one_autofix only reads db_path + env, no state needed.
                OperatorFlow._try_run_one_autofix(flow, db_path=db_path)
                fixed_one = True
                result["dispatched"] = True
            except Exception as exc:  # noqa: BLE001
                log.exception("coder_agent.dispatch_failed")
                result["dispatched"] = False
                result["dispatch_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["dispatched"] = False
            result["reason"] = "single autofix per cycle"
        tools["complete_task"](task_id=tid, result=result, status="done")
        actions.append({"task_id": tid, **result})

    return CoderResult(
        success=True, actions_taken=actions,
        narrative=f"Processed {len(actions)} coder task(s); "
                  f"{'1 autofix dispatched' if fixed_one else 'no dispatch'}.",
        turns_used=len(actions), elapsed_s=time.time() - started,
        fallback_used=False,
    )
