#!/usr/bin/env python3
"""Run the weekend ResearchCrew."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from src.config import load_config
from src.crews.research_crew import ResearchCrew
from src.db.models import init_db
from src.logging_setup import configure_logging

load_dotenv(REPO_ROOT / ".env")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run weekend Judas research crew")
    p.add_argument("--symbol", default="MGC")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    configure_logging(level=args.log_level, log_dir=REPO_ROOT / "logs", log_file="research_crew.log")
    log = logging.getLogger("research_main")
    db_path = REPO_ROOT / cfg.db_path
    init_db(db_path)
    os.environ["JUDAS_DB_PATH"] = str(db_path)
    log.info("research_crew.start", extra={"symbol": args.symbol.upper()})
    crew = ResearchCrew(symbol=args.symbol.upper(), verbose=cfg.crew.verbose)
    crew.kickoff(inputs={"symbol": args.symbol.upper()})
    log.info("research_crew.finished", extra={"symbol": args.symbol.upper()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
