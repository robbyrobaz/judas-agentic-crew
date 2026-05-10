#!/usr/bin/env python3
"""Coder specialist runner — triggered by Operator delegation, no timer."""
from __future__ import annotations

import os
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
    result = run_coder_decision(db_path=db_path)
    print(
        f"coder: success={result.success} tasks_processed={len(result.actions_taken)} "
        f"elapsed={result.elapsed_s:.1f}s fallback={result.fallback_used}"
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
