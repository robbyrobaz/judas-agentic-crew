#!/usr/bin/env python3
"""Trader specialist runner — invoked by judas-trader.timer (hourly poll)."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_setup import configure_logging  # noqa: E402
from src.research.trader_agent import run_trader_decision  # noqa: E402


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    # Self-gate: skip if no pending trader tasks
    pending = sqlite3.connect(db_path).execute(
        "SELECT COUNT(*) FROM agent_tasks WHERE team='trader' AND status='open'"
    ).fetchone()[0]
    if pending == 0:
        print("trader: no pending tasks — skipping")
        return 0
    # Bound the ReAct loop (default 0 = unlimited turns → cache-read blowup). Jun 18.
    result = run_trader_decision(
        db_path=db_path,
        # UNCAPPED (2026-06-23): no artificial turn/time limit. Wall-clock ceiling
        # is the systemd TimeoutStartSec (15min). 0 = unlimited.
        turn_budget=int(os.environ.get("JUDAS_TURN_BUDGET", "0")),
        time_budget_s=int(os.environ.get("JUDAS_TIME_BUDGET_S", "0")),
    )
    print(
        f"trader: success={result.success} actions={len(result.actions_taken)} "
        f"turns={result.turns_used} elapsed={result.elapsed_s:.1f}s "
        f"fallback={result.fallback_used} error={result.error}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
