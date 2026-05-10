"""OperatorFlow — daily autonomous review brain.

Skeleton implementation per Phase 1 of ``AGENTIC_OPERATOR_PLAN.md``. The leaf
steps are intentionally stubs; real logic lands in Phases 2/3/4/5.

State is persisted to a SQLite database via CrewAI's ``@persist`` decorator.
The persistence path is controlled by the ``JUDAS_OPERATOR_STATE_DB``
environment variable and defaults to ``outputs/flow_state.db`` resolved
relative to the repository root (the parent of this package's parent).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crewai.flow.flow import Flow, FlowState, listen, router, start
from crewai.flow.persistence import persist
from crewai.flow.persistence.sqlite import SQLiteFlowPersistence

from src.logging_setup import configure_logging

_ET_TZ = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    """Module-level clock seam — tests monkeypatch this for weekend detection."""
    return datetime.now(_ET_TZ)

log = logging.getLogger(__name__)

# Stable flow id so successive runs resume the same state row.
OPERATOR_FLOW_ID = "judas-operator-flow-singleton"

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _state_db_path() -> Path:
    """Resolve the persistence DB path.

    Honours ``JUDAS_OPERATOR_STATE_DB`` and otherwise falls back to
    ``<repo_root>/outputs/flow_state.db``.
    """
    override = os.environ.get("JUDAS_OPERATOR_STATE_DB")
    if override:
        return Path(override).expanduser().resolve()
    return _REPO_ROOT / "outputs" / "flow_state.db"


def _resolve_judas_db_path() -> str:
    """Resolve the judas_crew.db path used for live review."""
    override = os.environ.get("JUDAS_DB_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(_REPO_ROOT / "judas_crew.db")


def _open_autofix_hashes(*, db_path: str) -> set[str]:
    """Return symptom_hashes for auto_fixes rows where operator_decision IS NULL."""
    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return set()
    try:
        try:
            rows = conn.execute(
                "SELECT symptom_hash FROM auto_fixes WHERE operator_decision IS NULL"
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {row[0] for row in rows}
    finally:
        conn.close()


def _detect_pending_symptoms(*, db_path: str) -> list:
    """Return symptoms not already represented by an open auto_fixes row."""
    try:
        from src.research.symptoms import detect_all_symptoms
    except Exception:  # noqa: BLE001
        log.exception("operator.symptoms.import_failed")
        return []
    try:
        symptoms = detect_all_symptoms(db_path=db_path, repo_root=str(_REPO_ROOT))
    except Exception:  # noqa: BLE001
        log.exception("operator.symptoms.detect_failed")
        return []
    open_hashes = _open_autofix_hashes(db_path=db_path)
    return [s for s in symptoms if s.hash not in open_hashes]


def _decide_explore_or_noop(*, db_path: str) -> tuple[str, str | None]:
    """Decide between 'explore' and 'noop' when there is no retire-list.

    Triggers (any one fires explore):
      a) No turnover in active_strategies for 7+ days.
      b) Recent regime tag changed vs. the prior brief.
      c) Last brief has non-empty surprises.
      d) It's Saturday or Sunday in America/New_York.

    Returns (decision, reason). reason is None when decision == 'noop'.
    """
    # (d) weekend short-circuit — no DB needed.
    try:
        weekday = _now_et().weekday()  # Mon=0..Sun=6
    except Exception:  # noqa: BLE001 — extreme defensive
        weekday = 0
    if weekday >= 5:
        return "explore", "weekend_research"

    try:
        from src.research.explore import gather_explore_context

        ctx = gather_explore_context(db_path=db_path)
    except Exception:  # noqa: BLE001
        log.exception("operator.classify.context_failed")
        return "noop", None

    briefs = ctx.get("briefs_7d") or []
    most_recent = briefs[0] if briefs else None
    prior = briefs[1] if len(briefs) > 1 else None

    # (c) surprises in last brief
    if most_recent and (most_recent.get("n_surprises") or 0) > 0:
        return "explore", "recent_surprises"

    # (b) regime change since prior brief
    if most_recent and prior:
        cur_regime = (most_recent.get("regime") or {})
        prev_regime = (prior.get("regime") or {})
        if (
            cur_regime.get("vol_regime") != prev_regime.get("vol_regime")
            or cur_regime.get("trend") != prev_regime.get("trend")
        ):
            return "explore", "regime_change"

    # (a) no turnover in 7d
    if ctx.get("no_turnover_7d"):
        return "explore", "stale_active_set"

    return "noop", None


def _build_persistence() -> SQLiteFlowPersistence:
    """Build a SQLite persistence backend rooted at the configured path."""
    db_path = _state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteFlowPersistence(str(db_path))


class OperatorState(FlowState):
    """Typed state for OperatorFlow.

    Inherits ``id`` from ``FlowState`` so ``@persist`` can key rows.
    """

    last_run_utc: str | None = None
    findings: dict | None = None
    decision: str | None = None  # one of: retire | explore | fix_bug | noop | None
    cycle_count: int = 0


@persist(_build_persistence())
class OperatorFlow(Flow[OperatorState]):
    """Daily operator review flow.

    All branch leaves are stubs in Phase 1; the wiring (start → router →
    listeners) is real so persistence and the systemd unit can be exercised
    end-to-end.
    """

    @start()
    def morning_review(self) -> str:
        """PM agent runs the morning review.

        The agent has the loose "make money" mandate and acts via the tool
        palette (atomic registry helpers + deterministic broker path).
        Actions happen *inside* run_pm_decision via tool calls, so this step
        does not need a separate retire_step. We still route to ``fix_bug``
        when symptom detection finds something for the autofix harness; the
        PM agent doesn't write code.
        """
        self.state.cycle_count += 1
        self.state.last_run_utc = datetime.now(timezone.utc).isoformat()

        from src.research import operator_agent

        db_path = _resolve_judas_db_path()
        try:
            op_result = operator_agent.run_operator_decision(db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("operator.morning_review.operator_agent_failed")
            op_result = None

        pm_payload: dict | None
        if op_result is None:
            pm_payload = {
                "success": False,
                "actions_taken": [],
                "delegations": [],
                "narrative": "Operator agent crashed — see logs.",
                "turns_used": 0,
                "elapsed_s": 0.0,
                "fallback_used": True,
                "error": "operator_agent crashed",
            }
        else:
            pm_payload = {
                "success": op_result.success,
                "actions_taken": [
                    {
                        "action": a.action,
                        "target_id": a.target_id,
                        "payload": a.payload,
                        "rationale": a.rationale,
                        "tool_result": a.tool_result,
                    }
                    for a in op_result.actions_taken
                ],
                "delegations": [
                    {
                        "team": d.team,
                        "action": d.action,
                        "payload": d.payload,
                        "rationale": d.rationale,
                        "urgency": d.urgency,
                        "task_id": d.task_id,
                    }
                    for d in op_result.delegations
                ],
                "narrative": op_result.narrative,
                "turns_used": op_result.turns_used,
                "elapsed_s": op_result.elapsed_s,
                "fallback_used": op_result.fallback_used,
                "error": op_result.error,
            }

        pending_symptoms = _detect_pending_symptoms(db_path=db_path)
        if pending_symptoms:
            self.state.decision = "fix_bug"
            self.state.findings = {
                "pm_result": pm_payload,
                "pending_symptoms": [
                    {
                        "category": s.category,
                        "hash": s.hash,
                        "summary": s.summary,
                        "evidence": s.evidence,
                    }
                    for s in pending_symptoms
                ],
            }
        else:
            self.state.decision = "noop"
            self.state.findings = {"pm_result": pm_payload}

        log.info(
            "operator.morning_review.complete",
            extra={
                "cycle_count": self.state.cycle_count,
                "last_run_utc": self.state.last_run_utc,
                "decision": self.state.decision,
                "n_actions": len(pm_payload["actions_taken"]),
                "fallback_used": pm_payload["fallback_used"],
            },
        )
        return self.state.decision

    @router(morning_review)
    def classify(self, finding_signal: str) -> str:
        """Echo the upstream signal — real classification lands in later phases."""
        log.info("operator.classify.route", extra={"signal": finding_signal})
        return finding_signal

    @listen("retire")
    def retire_step(self) -> None:
        """No-op shim — kept for routing fallback compatibility.

        The PM agent (Phase 6+) takes its own retire actions via the
        ``retire_strategy`` tool inside ``run_pm_decision``. If anything
        routes here with a non-empty retire_list, it means a caller bypassed
        the PM agent — log loud and still write the brief.
        """
        findings = self.state.findings or {}
        retire_list = findings.get("retire_list") or []
        if retire_list:
            log.warning(
                "operator.retire_step.bypassed_pm_agent",
                extra={"n_retire": len(retire_list)},
            )
        else:
            log.info("operator.retire_step.noop")
        try:
            self.write_brief_step()
        except Exception:  # noqa: BLE001
            log.exception("operator.retire_step.brief_failed")

    @listen("explore")
    def explore_step(self) -> None:
        """Run an exploratory experiment, then write the daily brief.

        Errors must NOT crash the flow — fall through to write_brief_step.
        """
        findings = self.state.findings or {}
        try:
            from src.research.explore import (
                gather_explore_context,
                plan_experiment,
                execute_plan,
            )

            db_path = _resolve_judas_db_path()
            context = gather_explore_context(db_path=db_path)
            plan = plan_experiment(context=context)
            outcome = execute_plan(plan=plan, db_path=db_path)
            experiment_record = {
                "plan": plan.to_dict(),
                "outcome": outcome,
            }
            log.info(
                "operator.explore_step.complete",
                extra={
                    "tool": plan.tool,
                    "symbol": plan.symbol,
                    "fallback_used": plan.fallback_used,
                    "ok": outcome.get("ok"),
                    "experiment_id": outcome.get("experiment_id"),
                    "candidate_id": outcome.get("candidate_id"),
                    "explore_reason": findings.get("explore_reason"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("operator.explore_step.failed")
            experiment_record = {
                "plan": None,
                "outcome": {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            }

        # Stash on state so the brief can mention what ran.
        findings["experiment"] = experiment_record
        self.state.findings = findings

        # Always run the brief regardless of explore outcome.
        self.write_brief_step()

    @listen("fix_bug")
    def fix_bug_step(self) -> None:
        """Record detected symptoms in auto_fixes, then dispatch one autofix
        attempt; ALWAYS write the daily brief at the end so the operator's
        primary surface never goes dark on a fix-bug day.
        """
        try:
            self._do_fix_bug_step()
        finally:
            try:
                self.write_brief_step()
            except Exception:  # noqa: BLE001
                log.exception("operator.fix_bug_step.brief_failed")

    def _do_fix_bug_step(self) -> None:
        import sqlite3

        findings = self.state.findings or {}
        symptoms = findings.get("pending_symptoms") or []
        if not symptoms:
            # Re-detect defensively (covers callers that route here directly).
            try:
                fresh = _detect_pending_symptoms(db_path=_resolve_judas_db_path())
                symptoms = [
                    {
                        "category": s.category,
                        "hash": s.hash,
                        "summary": s.summary,
                        "evidence": s.evidence,
                    }
                    for s in fresh
                ]
            except Exception:  # noqa: BLE001
                log.exception("operator.fix_bug_step.detect_failed")
                symptoms = []

        if not symptoms:
            log.info("operator.fix_bug_step.no_symptoms")
            return

        from src.db.models import init_db

        db_path = _resolve_judas_db_path()
        try:
            init_db(db_path)
        except Exception:  # noqa: BLE001
            log.exception("operator.fix_bug_step.init_db_failed")
            return

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        inserted = 0
        skipped = 0
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                for sym in symptoms:
                    try:
                        conn.execute(
                            """
                            INSERT INTO auto_fixes (
                                started_at_utc, symptom_category, symptom_hash,
                                symptom_summary, status
                            ) VALUES (?, ?, ?, ?, 'detected')
                            """,
                            (
                                now,
                                sym.get("category"),
                                sym.get("hash"),
                                sym.get("summary"),
                            ),
                        )
                        inserted += 1
                    except sqlite3.IntegrityError:
                        # Unique partial index — already an open row for this hash.
                        skipped += 1
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            log.exception("operator.fix_bug_step.db_failed")
            return

        log.info(
            "operator.fix_bug_step.recorded",
            extra={
                "symptoms_total": len(symptoms),
                "inserted": inserted,
                "skipped_dupes": skipped,
                "categories": sorted({s.get("category") for s in symptoms if s.get("category")}),
            },
        )

        # Phase 3 wire-up: dispatch ONE autofix attempt per flow invocation.
        # Trigger gates from autofix_executor.can_autofix() enforce paper-safe
        # conditions (autofix.disable, market closed, no open positions,
        # MAX_AUTOFIXES_PER_DAY).
        try:
            self._try_run_one_autofix(db_path=db_path)
        except Exception:  # noqa: BLE001
            log.exception("operator.fix_bug_step.autofix_dispatch_failed")

    def _try_run_one_autofix(self, *, db_path: str) -> None:
        """Pick the oldest 'detected' auto_fixes row and try to fix it via
        worktree → M2.7 harness → commit+push. Updates the row with results.

        Inhibit via ``JUDAS_AUTOFIX_INHIBIT=1`` — the test suite sets this so
        unit tests that exercise just the symptom-recording path don't pay
        for a real harness invocation.
        """
        if os.environ.get("JUDAS_AUTOFIX_INHIBIT") == "1":
            log.info("operator.autofix.inhibited_by_env")
            return
        import sqlite3
        from src.research.autofix_executor import (
            ALLOWLIST_PATTERNS,
            DENYLIST_PATTERNS,
            can_autofix,
            cleanup_worktree,
            commit_and_push,
            create_autofix_worktree,
            install_denylist_hook,
        )
        from src.research.autofix_harness import run_harness

        allowed, reason = can_autofix()
        if not allowed:
            log.info("operator.autofix.gated", extra={"reason": reason})
            return

        repo_root = str(_REPO_ROOT)
        # Find the most recent 'detected' symptom we haven't tried.
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, symptom_category, symptom_hash, symptom_summary
                FROM auto_fixes
                WHERE status = 'detected' AND operator_decision IS NULL
                ORDER BY started_at_utc DESC
                LIMIT 1
                """
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            log.exception("operator.autofix.select_failed")
            return
        if row is None:
            log.info("operator.autofix.no_detected_rows")
            return

        autofix_id = int(row["id"])
        symptom_category = row["symptom_category"]
        symptom_summary = row["symptom_summary"]
        slug = (symptom_category or "fix").replace(" ", "-")[:32]

        try:
            ctx = create_autofix_worktree(
                autofix_id=autofix_id,
                symptom_slug=slug,
                repo_root=repo_root,
            )
            install_denylist_hook(worktree_path=ctx.worktree_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("operator.autofix.worktree_failed")
            self._update_autofix(
                db_path=db_path, autofix_id=autofix_id,
                status="error", test_result=None, diff_summary=None,
                files_changed_json=None, prompt=None, branch_name=None,
                worktree_path=None, pushed=0, finished=True,
                test_output_tail=f"worktree creation failed: {exc}",
            )
            return

        prompt = (
            f"Symptom category: {symptom_category}\n"
            f"Summary: {symptom_summary}\n\n"
            f"Diagnose the root cause within the allowlisted files only. "
            f"Apply the minimal patch that resolves the symptom and makes "
            f"pytest pass. If the fix requires touching a denylisted file, "
            f"explain why and stop.\n"
        )

        # Mark running before harness call.
        self._update_autofix(
            db_path=db_path, autofix_id=autofix_id,
            status="running", branch_name=ctx.branch_name,
            worktree_path=ctx.worktree_path, prompt=prompt,
        )

        try:
            result = run_harness(
                prompt=prompt,
                worktree_path=ctx.worktree_path,
                allowlist=list(ALLOWLIST_PATTERNS),
                denylist=list(DENYLIST_PATTERNS),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("operator.autofix.harness_failed")
            cleanup_worktree(worktree_path=ctx.worktree_path,
                             branch_name=ctx.branch_name, keep_branch=True)
            self._update_autofix(
                db_path=db_path, autofix_id=autofix_id,
                status="error", test_result="error",
                test_output_tail=f"harness exception: {exc}",
                finished=True,
            )
            return

        files_changed_json = None
        try:
            import json as _json
            files_changed_json = _json.dumps(result.files_changed)
        except Exception:  # noqa: BLE001
            files_changed_json = "[]"

        if not result.success:
            log.warning("operator.autofix.harness_no_fix",
                        extra={"error": result.error, "test_passed": result.test_passed})
            cleanup_worktree(worktree_path=ctx.worktree_path,
                             branch_name=ctx.branch_name, keep_branch=False)
            self._update_autofix(
                db_path=db_path, autofix_id=autofix_id,
                status="error",
                test_result="failed" if not result.test_passed else "skipped",
                diff_summary=result.diff_summary,
                files_changed_json=files_changed_json,
                test_output_tail=(result.test_output_tail or result.error or "")[:4000],
                finished=True,
            )
            return

        # Patch applied + tests passed. Commit + push.
        commit_message = f"autofix({symptom_category}): {symptom_summary[:60]}"
        push_result = commit_and_push(
            worktree_path=ctx.worktree_path,
            branch_name=ctx.branch_name,
            message=commit_message,
        )
        cleanup_worktree(worktree_path=ctx.worktree_path,
                         branch_name=ctx.branch_name, keep_branch=True)

        if push_result.get("denied"):
            log.warning("operator.autofix.denylist_hook_rejected",
                        extra=push_result)
            self._update_autofix(
                db_path=db_path, autofix_id=autofix_id,
                status="denied", test_result="denied",
                diff_summary=result.diff_summary,
                files_changed_json=files_changed_json,
                test_output_tail=(push_result.get("error") or "denylist hit")[:4000],
                pushed=0, finished=True,
            )
            return

        self._update_autofix(
            db_path=db_path, autofix_id=autofix_id,
            status="completed",
            test_result="passed" if result.test_passed else "failed",
            diff_summary=result.diff_summary,
            files_changed_json=files_changed_json,
            test_output_tail=(result.test_output_tail or "")[:4000],
            pushed=1 if push_result.get("pushed") else 0,
            finished=True,
        )
        log.warning("operator.autofix.completed",
                    extra={"autofix_id": autofix_id,
                           "branch": ctx.branch_name,
                           "pushed": push_result.get("pushed")})

    @staticmethod
    def _update_autofix(*, db_path: str, autofix_id: int, **fields) -> None:
        """Patch arbitrary auto_fixes columns by id. `finished=True` sets
        finished_at_utc to now."""
        import sqlite3
        finished = fields.pop("finished", False)
        if finished:
            fields["finished_at_utc"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [autofix_id]
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    f"UPDATE auto_fixes SET {cols} WHERE id = ?", vals
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            log.exception("operator.autofix.update_failed")

    @listen("noop")
    def write_brief_step(self) -> None:
        """Compose + persist the daily brief for yesterday (America/New_York).

        Errors must NOT crash the flow — log and continue.
        """
        try:
            from src.research.brief import (
                compose_daily_brief,
                persist_daily_brief,
                _yesterday_et,
            )
        except Exception:  # noqa: BLE001
            log.exception("operator.write_brief_step.import_failed")
            return

        db_path = _resolve_judas_db_path()
        findings = self.state.findings or {}
        pm_result = findings.get("pm_result")
        try:
            brief_date = _yesterday_et()
            composed = compose_daily_brief(
                db_path=db_path, brief_date=brief_date, pm_result=pm_result,
            )
            brief_id = persist_daily_brief(
                db_path=db_path,
                brief_date=brief_date,
                content_md=composed["content_md"],
                summary_json=composed["summary"],
            )
            log.info(
                "operator.write_brief_step.persisted",
                extra={"brief_date": brief_date, "brief_id": brief_id},
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "operator.write_brief_step.failed_noop_with_brief_skipped"
            )


def main() -> None:
    """CLI entry point: run a single OperatorFlow cycle, resuming prior state."""
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    log.info("operator.main.start", extra={"db_path": str(_state_db_path())})
    flow = OperatorFlow()
    flow.kickoff(inputs={"id": OPERATOR_FLOW_ID})
    log.info(
        "operator.main.complete",
        extra={
            "cycle_count": flow.state.cycle_count,
            "last_run_utc": flow.state.last_run_utc,
            "decision": flow.state.decision,
        },
    )


if __name__ == "__main__":
    main()
