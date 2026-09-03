#!/usr/bin/env python3
"""Registrar specialist runner — invoked by judas-registrar.timer (5-min poll)."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_setup import configure_logging  # noqa: E402
from src.research.registrar_agent import run_registrar_decision  # noqa: E402


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    with sqlite3.connect(db_path) as conn:
        candidates = conn.execute(
            "SELECT COUNT(*) FROM strategy_candidates WHERE status='candidate'"
        ).fetchone()[0]
        tasks = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE team='registrar' AND status='open'"
        ).fetchone()[0]
    if candidates == 0 and tasks == 0:
        print("registrar: no candidates or pending tasks — skipping")
        return 0
    result = run_registrar_decision(
        db_path=db_path,
        turn_budget=int(os.environ.get("JUDAS_TURN_BUDGET", "6")),
        time_budget_s=int(os.environ.get("JUDAS_TIME_BUDGET_S", "300")),
    )
    print(
        f"registrar: success={result.success} actions={len(result.actions_taken)} "
        f"turns={result.turns_used} elapsed={result.elapsed_s:.1f}s "
        f"fallback={result.fallback_used} error={result.error}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
