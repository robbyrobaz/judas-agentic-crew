"""DB tool wrappers — CrewAI @tool wrappers around SQLite operations.

Four tools:
  db_daily_pnl_tool       — today's realized P&L sum
  db_daily_activity_tool  — today's executed trade count
  db_open_positions_tool  — count and list of open trades
  db_save_signal_tool     — persist a signal to the signals table
  db_save_trade_tool      — persist a trade to the trades table
  db_save_research_experiment_tool — persist a research experiment summary
  db_recent_research_tool — fetch recent experiment summaries
  db_active_strategy_tool — fetch the current active paper strategy
  db_create_strategy_candidate_tool — persist a promotion candidate
  db_promote_strategy_candidate_tool — promote a candidate into active paper use

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


def _json_loads(value: str | dict | list | None):
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


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


@tool("db_daily_activity_tool")
def db_daily_activity_tool(input_json: str = "{}") -> str:
    """Return today's executed trade count from the trades table.

    No input required.

    Returns JSON:
    {
      "trades_today": int,
      "date": "2026-05-08"
    }
    """
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS trades_today
                FROM trades
                WHERE opened_at LIKE ?
                """,
                (f"{today}%",),
            ).fetchone()
            trades_today = int(row["trades_today"]) if row else 0
        log.info("db.daily_activity", extra={"date": today, "trades_today": trades_today})
        return json.dumps({"trades_today": trades_today, "date": today})
    except Exception as e:
        log.error("db.daily_activity.error", extra={"error": str(e)})
        return json.dumps({"trades_today": 0, "date": today, "error": str(e)})


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
                    (ts_utc, symbol, strategy_id, strategy_family, strategy_version,
                     direction, quality_score, risk_decision,
                     entry, stop, target, rationale, agent_notes, raw_llm_output, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts_utc", now),
                    data.get("symbol", ""),
                    data.get("strategy_id"),
                    data.get("strategy_family"),
                    data.get("strategy_version"),
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
                    (signal_id, strategy_id, strategy_family, strategy_version,
                     ibkr_order_id, symbol, direction, qty,
                     entry_fill, stop_price, target_price, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    data.get("signal_id"),
                    data.get("strategy_id"),
                    data.get("strategy_family"),
                    data.get("strategy_version"),
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


@tool("db_save_research_experiment_tool")
def db_save_research_experiment_tool(input_json: str) -> str:
    """Persist a research experiment summary to the research_experiments table."""
    try:
        data = json.loads(input_json) if isinstance(input_json, str) else input_json
    except json.JSONDecodeError as e:
        return json.dumps({"experiment_id": -1, "error": f"Invalid JSON: {e}"})

    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO research_experiments
                    (ts_utc, symbol, experiment_type, name, status,
                     metrics_json, parameters_json, artifacts_json, summary, recommendations_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("ts_utc", now),
                    data.get("symbol", ""),
                    data.get("experiment_type", "research_run"),
                    data.get("name", "unnamed_experiment"),
                    data.get("status", "completed"),
                    json.dumps(data.get("metrics", {})),
                    json.dumps(data.get("parameters", {})),
                    json.dumps(data.get("artifacts", {})),
                    data.get("summary", ""),
                    json.dumps(data.get("recommendations", [])),
                ),
            )
            experiment_id = cur.lastrowid
        log.info(
            "db.research_experiment_saved",
            extra={"experiment_id": experiment_id, "symbol": data.get("symbol")},
        )
        return json.dumps({"experiment_id": experiment_id})
    except Exception as e:
        log.error("db.save_research_experiment.error", extra={"error": str(e)})
        return json.dumps({"experiment_id": -1, "error": str(e)})


@tool("db_recent_research_tool")
def db_recent_research_tool(input_json: str = "{}") -> str:
    """Return recent research experiment summaries for a symbol."""
    try:
        data = json.loads(input_json) if isinstance(input_json, str) else input_json
    except json.JSONDecodeError:
        data = {}
    symbol = str(data.get("symbol", "")).upper()
    limit = int(data.get("limit", 10))
    try:
        with _get_conn() as conn:
            if symbol:
                rows = conn.execute(
                    """
                    SELECT id, ts_utc, symbol, experiment_type, name, status, summary, metrics_json
                    FROM research_experiments
                    WHERE symbol = ?
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, ts_utc, symbol, experiment_type, name, status, summary, metrics_json
                    FROM research_experiments
                    ORDER BY ts_utc DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            experiments = [
                {
                    "id": row["id"],
                    "ts_utc": row["ts_utc"],
                    "symbol": row["symbol"],
                    "experiment_type": row["experiment_type"],
                    "name": row["name"],
                    "status": row["status"],
                    "summary": row["summary"],
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                }
                for row in rows
            ]
        return json.dumps({"experiments": experiments})
    except Exception as e:
        log.error("db.recent_research.error", extra={"error": str(e)})
        return json.dumps({"experiments": [], "error": str(e)})


@tool("db_active_strategy_tool")
def db_active_strategy_tool(input_json: str = "{}") -> str:
    """Return the current active paper strategy for a symbol/family."""
    try:
        data = _json_loads(input_json)
    except Exception as e:
        return json.dumps({"strategy": None, "error": f"Invalid JSON: {e}"})

    from src.strategy_registry import get_active_or_default

    symbol = str(data.get("symbol", "MGC")).upper()
    strategy_family = str(data.get("strategy_family", "judas_1h"))
    try:
        active = get_active_or_default(symbol=symbol, strategy_family=strategy_family)
        return json.dumps(
            {
                "strategy": {
                    "id": active.id,
                    "symbol": active.symbol,
                    "strategy_family": active.strategy_family,
                    "version": active.version,
                    "params": active.params,
                    "metrics": active.metrics,
                    "source_candidate_id": active.source_candidate_id,
                    "state": active.state,
                    "activated_at_utc": active.activated_at_utc,
                    "notes": active.notes,
                }
            }
        )
    except Exception as e:
        log.error("db.active_strategy.error", extra={"error": str(e), "symbol": symbol})
        return json.dumps({"strategy": None, "error": str(e)})


@tool("db_create_strategy_candidate_tool")
def db_create_strategy_candidate_tool(input_json: str) -> str:
    """Persist a strategy candidate generated by research."""
    try:
        data = _json_loads(input_json)
    except Exception as e:
        return json.dumps({"candidate_id": -1, "error": f"Invalid JSON: {e}"})

    from src.strategy_registry import create_candidate

    try:
        candidate_id = create_candidate(
            symbol=str(data.get("symbol", "MGC")).upper(),
            strategy_family=str(data.get("strategy_family", "judas_1h")),
            params=data.get("params") or {},
            metrics=data.get("metrics") or {},
            decision=str(data.get("decision", "candidate")),
            rationale=str(data.get("rationale", "")),
            status=str(data.get("status", "candidate")),
            source_experiment_id=int(data["source_experiment_id"]) if data.get("source_experiment_id") is not None else None,
        )
        return json.dumps({"candidate_id": candidate_id})
    except Exception as e:
        log.error("db.create_strategy_candidate.error", extra={"error": str(e)})
        return json.dumps({"candidate_id": -1, "error": str(e)})


@tool("db_promote_strategy_candidate_tool")
def db_promote_strategy_candidate_tool(input_json: str) -> str:
    """Promote a candidate into the active paper strategy registry."""
    try:
        data = _json_loads(input_json)
    except Exception as e:
        return json.dumps({"active_strategy_id": -1, "error": f"Invalid JSON: {e}"})

    from src.strategy_registry import promote_candidate

    try:
        candidate_id = int(data.get("candidate_id"))
        active = promote_candidate(candidate_id, notes=str(data.get("notes", "Automated paper promotion.")))
        return json.dumps(
            {
                "active_strategy_id": active.id,
                "symbol": active.symbol,
                "strategy_family": active.strategy_family,
                "version": active.version,
            }
        )
    except Exception as e:
        log.error("db.promote_strategy_candidate.error", extra={"error": str(e)})
        return json.dumps({"active_strategy_id": -1, "error": str(e)})
