#!/usr/bin/env python3
"""Entry point for the Judas Agentic Crew.

Usage:
    python main.py --symbol MGC

Systemd oneshot: fires once per hour during London and NY sessions.
"""
from __future__ import annotations

import argparse
import json
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
        "--all-symbols",
        action="store_true",
        help="Run once for every symbol enabled in config.yaml",
    )
    p.add_argument(
        "--doctor",
        action="store_true",
        help="Run connectivity checks for config, DB, LLM, and IBKR historical data",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    p.add_argument(
        "--runtime",
        default="auto",
        choices=["auto", "portfolio", "crew"],
        help="Execution runtime: active-strategy portfolio, legacy CrewAI trading crew, or auto-detect",
    )
    p.add_argument(
        "--seed-workshop",
        action="store_true",
        help="Import workshop buffet/backtest artifacts and seed active portfolio strategies before running",
    )
    p.add_argument(
        "--eval-only",
        action="store_true",
        help="Portfolio runtime only: evaluate active strategies and record skips, but do not place paper orders",
    )
    return p.parse_args()


def _run_doctor(symbol: str) -> int:
    log = logging.getLogger("doctor")
    summary: dict[str, object] = {"symbol": symbol, "checks": {}}

    from src.agents.judas_agents import build_llm
    from src.tools.ibkr_data import ibkr_data_tool
    from src.tools.session_tools import session_status_tool

    try:
        llm = build_llm()
        response = llm.call("Reply with exactly OK.")
        summary["checks"]["llm"] = {"ok": True, "response": str(response).strip()}
    except Exception as e:
        log.error("doctor.llm_failed", extra={"error": str(e)}, exc_info=True)
        summary["checks"]["llm"] = {"ok": False, "error": str(e)}

    try:
        payload = json.loads(ibkr_data_tool.run(input_json=json.dumps({"symbol": symbol})))
        ok = bool(payload.get("bars")) and not payload.get("error")
        summary["checks"]["ibkr_data"] = {
            "ok": ok,
            "bar_count": len(payload.get("bars", [])),
            "prior_high": payload.get("prior_high"),
            "prior_low": payload.get("prior_low"),
            "error": payload.get("error"),
        }
    except Exception as e:
        log.error("doctor.ibkr_failed", extra={"error": str(e)}, exc_info=True)
        summary["checks"]["ibkr_data"] = {"ok": False, "error": str(e)}

    try:
        payload = json.loads(session_status_tool.run(input_json="{}"))
        summary["checks"]["session_status"] = {
            "ok": True,
            "can_trade_now": payload.get("can_trade_now"),
            "active_session": payload.get("active_session"),
            "reason": payload.get("reason"),
        }
    except Exception as e:
        log.error("doctor.session_failed", extra={"error": str(e)}, exc_info=True)
        summary["checks"]["session_status"] = {"ok": False, "error": str(e)}

    print(json.dumps(summary, indent=2))
    return 0 if all(check.get("ok") for check in summary["checks"].values()) else 1


def _preflight_session_gate() -> dict[str, object]:
    from src.tools.session_tools import session_status_tool

    return json.loads(session_status_tool.run(input_json="{}"))


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

        if args.seed_workshop:
            from scripts.import_workshop_seed import main as import_workshop_seed_main

            import_workshop_seed_main()

        if args.doctor:
            return _run_doctor(symbol)

        # Session gate intentionally NOT enforced at the top level. The
        # individual strategy detectors (Judas, etc.) have their own
        # session_filter param that decides whether THEY want to fire in
        # the current window. Don't pre-empt that decision here by
        # skipping the whole scan during maintenance breaks or off-window
        # times — let the strategies see every hour and decide.
        session_status = _preflight_session_gate()
        log.info(
            "judas_crew.session_state",
            extra={
                "symbol": symbol,
                "reason": session_status.get("reason"),
                "can_trade_now_per_session_tool": session_status.get("can_trade_now"),
                "active_session": session_status.get("active_session"),
                "now_et": session_status.get("now_et"),
            },
        )

        runtime = args.runtime
        if runtime == "auto":
            from src.strategy_registry import list_active_strategies

            active = list_active_strategies()
            runtime = "portfolio" if active else "crew"

        if runtime == "portfolio":
            from src.portfolio_runtime import run_portfolio_scan

            result = run_portfolio_scan(
                db_path=str(db_path),
                host=cfg.ibkr.host,
                port=cfg.ibkr.port,
                data_client_id=cfg.ibkr.data_client_id,
                exec_client_id=cfg.ibkr.exec_client_id,
                max_new_trades=cfg.risk.max_trades_per_day,
                max_open_positions=cfg.risk.max_open_positions,
                max_trades_per_day=cfg.risk.max_trades_per_day,
                place_orders=not args.eval_only,
            )
            print(json.dumps({"status": "ok", "runtime": "portfolio", "result": result}, indent=2, default=str))
            log.info("portfolio_runtime.finished", extra={"fire_count": len(result.get("fires", []))})
        else:
            from src.crews.judas_crew import JudasCrew

            symbols = [s.symbol.upper() for s in cfg.symbols] if args.all_symbols else [symbol]
            for current_symbol in symbols:
                crew = JudasCrew(symbol=current_symbol, verbose=cfg.crew.verbose)
                result = crew.kickoff(inputs={"symbol": current_symbol})
                log.info(
                    "judas_crew.finished",
                    extra={"symbol": current_symbol, "result_type": type(result).__name__},
                )
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
