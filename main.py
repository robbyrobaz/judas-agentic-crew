#!/usr/bin/env python3
"""Entry point for the Judas Agentic Crew.

Usage:
    python main.py --symbol MGC

Systemd oneshot: fires once per hour during London and NY sessions.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Bootstrap: ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env")

from src.logging_setup import configure_logging
from src.db.models import init_db
from src.config import load_config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Judas Agentic Crew — ICT Judas Swing on IBKR paper")
    p.add_argument(
        "--symbol",
        default="MGC",
        help="Futures symbol to analyze (default: MGC)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()

    # Configure logging first so all subsequent imports see it
    log_dir = _REPO_ROOT / "logs"
    configure_logging(
        level=args.log_level,
        log_dir=log_dir,
        log_file="judas_crew.log",
    )
    log = logging.getLogger("main")
    log.info("judas_crew.start", extra={"symbol": symbol})

    try:
        # Load and validate config (raises ValueError if mode != paper)
        cfg = load_config()
        log.info("config.loaded", extra={"mode": cfg.mode, "symbol": symbol})

        # Initialize SQLite schema
        db_path = _REPO_ROOT / cfg.db_path
        init_db(db_path)
        log.info("db.initialized", extra={"db_path": str(db_path)})

        # Set DB path env var so db_tools can find it
        import os
        os.environ["JUDAS_DB_PATH"] = str(db_path)

        # Run the crew
        from src.crews.judas_crew import JudasCrew
        crew = JudasCrew(symbol=symbol, verbose=cfg.crew.verbose)
        result = crew.kickoff(inputs={"symbol": symbol})

        log.info("judas_crew.finished", extra={"symbol": symbol, "result_type": type(result).__name__})
        return 0

    except ValueError as e:
        log.error("judas_crew.config_error", extra={"error": str(e)})
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.info("judas_crew.interrupted")
        return 0
    except Exception as e:
        log.error("judas_crew.fatal_error", extra={"error": str(e)}, exc_info=True)
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
