"""DB tool wrappers — CrewAI @tool wrappers around SQLite operations.

Four tools:
  db_daily_pnl_tool       — today's realized P&L sum
  db_open_positions_tool  — count and list of open trades
  db_save_signal_tool     — persist a signal to the signals table
  db_save_trade_tool      — persist a trade to the trades table

DB path is read from the JUDAS_DB_PATH env var (default: judas_crew.db
in the repo root).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from crewai.tools import tool

log = logging.getLogger(__name__)

_DEFAULT_DB = str(Path(__file__).parent.parent.parent / "judas_crew.db")


def _db_path() -> str:
    return os.environ.get("JUDAS_DB_PATH", _DEFAULT_DB)


def _get_conn():
    """Import and return a get_conn context manager for the DB."""
    from src.db.models import get_conn
    return get_conn(_db_path())


@tool("db_daily_pnl_tool")
def db_daily_pnl_tool(input_json: str = "{}") -> str:
    """Return today's realized P&L sum from the trades table.

    No input required (pass '{}' or empty string).

    Returns JSON:
    {
      "daily_pnl": float,
      "trade_count": int,
      "date": "2026-05-08"
    }
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(pnl_dollars), 0.0) AS daily_pnl,
                    COUNT(*) AS trade_count
                FROM trades
                WHERE closed_at LIKE ?
                  AND status = 'closed'
                """,
                (f"{today}%",),
            ).fetchone()
            daily_pnl = float(row["daily_pnl"]) if row else 0.0
            trade_count = int(row["trade_count"]) if row else 0

        log.info("db.daily_pnl", extra={"date": today, "daily_pnl": daily_pnl, "trade_count": trade_count})
        return json.dumps({"daily_pnl": daily_pnl, "trade_count": trade_count, "date": today})
    except Exception as e:
        log.error("db.daily_pnl.error", extra={"error": str(e)})
        return json.dumps({"daily_pnl": 0.0, "trade_count": 0, "date": today, "error": str(e)})


@tool("db_open_positions_tool")
def db_open_positions_tool(input_json: str = "{}") -> str:
    """Return count and list of currently open trades.

    No input required (pass '{}' or empty string).

    Returns JSON:
    {
      "open_count": int,
      "positions": [{"symbol": ..., "direction": ..., "opened_at": ..., "entry_fill": ...}]
    }
    """
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT symbol, direction, opened_at, entry_fill
                FROM trades
                WHERE status = 'open'
                ORDER BY opened_at DESC
                """,
            ).fetchall()
            positions = [
                {
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "opened_at": r["opened_at"],
                    "entry_fill": r["entry_fill"],
                }
                for r in rows
            ]

        log.info("db.open_positions", extra={"open_count": len(positions)})
        return json.dumps({"open_count": len(positions), "positions": positions})
    except Exception as e:
        log.error("db.open_positions.error", extra={"error": str(e)})
        return json.dumps({"open_count": 0, "positions": [], "error": str(e)})


@tool("db_save_signal_tool")
def db_save_signal_tool(input_json: str) -> str:
    """Save a detected Judas signal to the signals table.

    Input JSON (all fields from SetupEvaluator / detector output):
    {
      "ts_utc": "2026-05-08T14:00:00Z",
      "symbol": "MGC",
      "direction": "short",
      "quality_score": 7,
      "risk_decision": "TRADE",
      "entry": 3220.50,
      "stop": 3225.80,
      "target": 3210.00,
      "rationale": "...",
      "agent_notes": "...",
      "raw_llm_output": "..."
    }

    Returns JSON: {"signal_id": int}
    """
    try:
        data = json.loads(input_json) if isinstance(input_json, str) else input_json
    except json.JSONDecodeError as e:
        return json.dumps({"signal_id": -1, "error": f"Invalid JSON: {e}"})

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO signals
                    (ts_utc, symbol, direction, quality_score, risk_decision,
                     entry, stop, target, rationale, agent_notes, raw_llm_output, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts_utc", now),
                    data.get("symbol", ""),
                    data.get("direction", ""),
                    data.get("quality_score"),
                    data.get("risk_decision"),
                    data.get("entry"),
                    data.get("stop"),
                    data.get("target"),
                    data.get("rationale"),
                    data.get("agent_notes"),
                    data.get("raw_llm_output"),
                    now,
                ),
            )
            signal_id = cur.lastrowid

        log.info("db.signal_saved", extra={"signal_id": signal_id, "symbol": data.get("symbol")})
        return json.dumps({"signal_id": signal_id})
    except Exception as e:
        log.error("db.save_signal.error", extra={"error": str(e)})
        return json.dumps({"signal_id": -1, "error": str(e)})


@tool("db_save_trade_tool")
def db_save_trade_tool(input_json: str) -> str:
    """Save a trade record to the trades table after order placement.

    Input JSON:
    {
      "signal_id": int,
      "ibkr_order_id": int,
      "symbol": "MGC",
      "direction": "short",
      "qty": 1,
      "entry_fill": 3220.50,
      "stop_price": 3225.80,
      "target_price": 3210.00
    }

    Returns JSON: {"trade_id": int}
    """
    try:
        data = json.loads(input_json) if isinstance(input_json, str) else input_json
    except json.JSONDecodeError as e:
        return json.dumps({"trade_id": -1, "error": f"Invalid JSON: {e}"})

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades
                    (signal_id, ibkr_order_id, symbol, direction, qty,
                     entry_fill, stop_price, target_price, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    data.get("signal_id"),
                    str(data.get("ibkr_order_id", "")),
                    data.get("symbol", ""),
                    data.get("direction", ""),
                    int(data.get("qty", 1)),
                    data.get("entry_fill"),
                    data.get("stop_price"),
                    data.get("target_price"),
                    now,
                ),
            )
            trade_id = cur.lastrowid

        log.info("db.trade_saved", extra={"trade_id": trade_id, "symbol": data.get("symbol")})
        return json.dumps({"trade_id": trade_id})
    except Exception as e:
        log.error("db.save_trade.error", extra={"error": str(e)})
        return json.dumps({"trade_id": -1, "error": str(e)})
