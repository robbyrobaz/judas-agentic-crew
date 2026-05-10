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
from dataclasses import asdict
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
        """Run live-review on all active strategies; route based on decisions."""
        self.state.cycle_count += 1
        self.state.last_run_utc = datetime.now(timezone.utc).isoformat()

        # Lazy import so Phase-1 tests that don't seed a DB still pass.
        from src.research.live_review import review_all_active_strategies

        db_path = _resolve_judas_db_path()
        try:
            results = review_all_active_strategies(db_path=db_path)
        except Exception:  # noqa: BLE001
            log.exception("operator.morning_review.failed")
            results = []

        retire_list = []
        review_summary = []
        for metrics, decision in results:
            entry = {
                "strategy_id": metrics.strategy_id,
                "strategy_name": metrics.strategy_name,
                "action": decision.action,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "fallback_used": decision.fallback_used,
            }
            review_summary.append(entry)
            if decision.action == "retire":
                retire_list.append(
                    {
                        "strategy_id": metrics.strategy_id,
                        "metrics": asdict(metrics),
                        "reason": decision.reason,
                    }
                )

        if retire_list:
            self.state.decision = "retire"
            self.state.findings = {
                "retire_list": retire_list,
                "review_summary": review_summary,
            }
        else:
            pending_symptoms = _detect_pending_symptoms(db_path=db_path)
            if pending_symptoms:
                self.state.decision = "fix_bug"
                self.state.findings = {
                    "review_summary": review_summary,
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
                decision, reason = _decide_explore_or_noop(db_path=db_path)
                self.state.decision = decision
                self.state.findings = {
                    "review_summary": review_summary,
                    "explore_reason": reason if decision == "explore" else None,
                }

        log.info(
            "operator.morning_review.complete",
            extra={
                "cycle_count": self.state.cycle_count,
                "last_run_utc": self.state.last_run_utc,
                "decision": self.state.decision,
                "n_reviewed": len(review_summary),
                "n_retire": len(retire_list),
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
        """Atomically retire each strategy flagged in findings.retire_list."""
        findings = self.state.findings or {}
        retire_list = findings.get("retire_list", [])
        if not retire_list:
            log.info("operator.retire_step.empty")
            return

        # Imported lazily so worker D can land its impl independently.
        from src import strategy_registry

        retire_fn = getattr(strategy_registry, "retire_strategy", None)
        if retire_fn is None:
            log.warning("operator.retire_step.registry_missing_retire_strategy")
            return

        for entry in retire_list:
            sid = entry.get("strategy_id")
            try:
                demotion_id = retire_fn(
                    strategy_id=int(sid),
                    reason=str(entry.get("reason", "auto-demotion")),
                    metrics_snapshot=entry.get("metrics", {}),
                )
                log.info(
                    "operator.retire_step.retired",
                    extra={
                        "strategy_id": sid,
                        "demotion_id": demotion_id,
                    },
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "operator.retire_step.failed", extra={"strategy_id": sid}
                )

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
        """Phase 3a — record detected symptoms in auto_fixes; do NOT fix.

        Workers I/J replace this step in Phase 3b/3c. Keep minimal.
        """
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
        try:
            brief_date = _yesterday_et()
            composed = compose_daily_brief(db_path=db_path, brief_date=brief_date)
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
