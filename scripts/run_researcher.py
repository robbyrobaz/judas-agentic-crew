#!/usr/bin/env python3
"""Researcher specialist runner — invoked by judas-researcher.timer."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.logging_setup import configure_logging  # noqa: E402
from src.research.researcher_agent import run_researcher_decision  # noqa: E402


def main() -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    db_path = os.environ.get(
        "JUDAS_DB_PATH",
        str(Path(__file__).resolve().parents[1] / "judas_crew.db"),
    )
    # Bound the ReAct loop: unbounded turns (the default 0) re-send the whole
    # growing conversation every tool call → cache-read token blowup. Jun 18.
    result = run_researcher_decision(
        db_path=db_path,
        turn_budget=int(os.environ.get("JUDAS_TURN_BUDGET", "25")),
        time_budget_s=int(os.environ.get("JUDAS_TIME_BUDGET_S", "600")),
    )
    print(
        f"researcher: success={result.success} actions={len(result.actions_taken)} "
        f"turns={result.turns_used} elapsed={result.elapsed_s:.1f}s "
        f"fallback={result.fallback_used} error={result.error}"
    )
    # A time-budget stop is a NORMAL early-stop (the ReAct loop is intentionally
    # bounded), NOT a crash. Only exit non-zero on a genuine error, so the
    # systemd unit doesn't sit `failed` (and mask real failures) every run.
    clean_budget_stop = bool(result.error) and result.error.startswith("time budget exhausted")
    return 0 if (result.success or clean_budget_stop) else 1


if __name__ == "__main__":
    raise SystemExit(main())
