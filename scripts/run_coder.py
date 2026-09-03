#!/usr/bin/env python3
"""Coder specialist runner — invoked by judas-coder.timer (hourly poll)."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_setup import configure_logging  # noqa: E402
from src.research.coder_agent import run_coder_decision  # noqa: E402


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    # Self-gate: skip if no pending coder tasks
    pending = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE team='coder' AND status='open'"
    ).fetchone()[0]
    if pending == 0:
        print("coder: no pending tasks — skipping")
        return 0
    result = run_coder_decision(db_path=db_path, turn_budget=1)
    print(
        f"coder: success={result.success} tasks_processed={len(result.actions_taken)} "
        f"elapsed={result.elapsed_s:.1f}s fallback={result.fallback_used}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
