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

from crewai.flow.flow import Flow, FlowState, listen, router, start
from crewai.flow.persistence import persist
from crewai.flow.persistence.sqlite import SQLiteFlowPersistence

from src.logging_setup import configure_logging

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
        """Stub morning review — increments cycle, stamps run, returns 'noop'."""
        self.state.cycle_count += 1
        self.state.last_run_utc = datetime.now(timezone.utc).isoformat()
        self.state.findings = {"stub": True}
        self.state.decision = "noop"
        log.info(
            "operator.morning_review.complete",
            extra={
                "cycle_count": self.state.cycle_count,
                "last_run_utc": self.state.last_run_utc,
            },
        )
        return "noop"

    @router(morning_review)
    def classify(self, finding_signal: str) -> str:
        """Echo the upstream signal — real classification lands in later phases."""
        log.info("operator.classify.route", extra={"signal": finding_signal})
        return finding_signal

    @listen("retire")
    def retire_step(self) -> None:
        """Stub — real demotion logic lands in Phase 2."""
        log.info("operator.retire_step.would_run")

    @listen("explore")
    def explore_step(self) -> None:
        """Stub — real adaptive explorer lands in Phase 5."""
        log.info("operator.explore_step.would_run")

    @listen("fix_bug")
    def fix_bug_step(self) -> None:
        """Stub — real autofix delegation lands in Phase 3."""
        log.info("operator.fix_bug_step.would_run")

    @listen("noop")
    def write_brief_step(self) -> None:
        """Stub — real daily brief lands in Phase 4. Always runs on the noop path."""
        log.info("operator.write_brief_step.would_run")


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
