#!/usr/bin/env python3
"""Reviewer specialist runner — invoked by judas-reviewer.timer (20-min poll)."""
from __future__ import annotations

import os
import sqlite3
import sys
import time
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
    state_path = Path(__file__).resolve().parents[1] / "data" / "reviewer_last_run"
    last_run = state_path.stat().st_mtime if state_path.exists() else 0.0
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_run))
    with sqlite3.connect(db_path) as conn:
        candidates = conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates WHERE status='candidate'"
        ).fetchone()[0]
        tasks = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE team='reviewer' AND status='open'"
        ).fetchone()[0]
        new_trades = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='closed' AND closed_at > ?", (since,)
        ).fetchone()[0]
    if candidates == 0 and tasks == 0 and new_trades == 0 and time.time() - last_run < 86400:
        print("reviewer: no new evidence or pending work — skipping")
        return 0
    result = run_reviewer_decision(
        db_path=db_path,
        turn_budget=int(os.environ.get("JUDAS_TURN_BUDGET", "8")),
        time_budget_s=int(os.environ.get("JUDAS_TIME_BUDGET_S", "600")),
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.touch()
    print(
        f"reviewer: success={result.success} actions={len(result.actions_taken)} "
        f"turns={result.turns_used} elapsed={result.elapsed_s:.1f}s "
        f"fallback={result.fallback_used} error={result.error}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
