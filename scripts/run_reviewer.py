#!/usr/bin/env python3
"""Reviewer specialist runner — invoked by judas-reviewer.timer (20-min poll)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_setup import configure_logging  # noqa: E402
from src.research.reviewer_agent import run_reviewer_decision  # noqa: E402


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    # Bound the ReAct loop (default 0 = unlimited turns → cache-read blowup). Jun 18.
    result = run_reviewer_decision(
        db_path=db_path,
        turn_budget=int(os.environ.get("JUDAS_TURN_BUDGET", "20")),
        time_budget_s=int(os.environ.get("JUDAS_TIME_BUDGET_S", "420")),
    )
    print(
        f"reviewer: success={result.success} actions={len(result.actions_taken)} "
        f"turns={result.turns_used} elapsed={result.elapsed_s:.1f}s "
        f"fallback={result.fallback_used} error={result.error}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
