"""Flask dashboard backend for monitoring and chatting with the Operator Manager."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from waitress import serve

from src.agents.judas_agents import build_llm
from src.config import load_config
from src.db.models import get_conn, init_db
from src.strategy_registry import list_active_strategies
from src.tools.session_tools import session_status_tool

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIST = REPO_ROOT / "dashboard" / "dist"
CHAT_HISTORY: list[dict[str, str]] = []

OPERATOR_MANAGER_PROMPT = (
    "You are the Operator Manager for the entire Judas system. You oversee both the live paper "
    "TradingCrew and the ResearchCrew. You have broad read access to current system state, timers, "
    "recent experiments, session status, and latest records. You do not bypass deterministic trading "
    "gates, do not imply live trading, and do not pretend unverified actions occurred. "
    "You help the operator understand what is running, what failed, what matters next, and when to act."
)


def _db_path() -> Path:
    cfg = load_config()
    path = REPO_ROOT / cfg.db_path
    init_db(path)
    os.environ["JUDAS_DB_PATH"] = str(path)
    return path


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _fetch_recent_signals(limit: int = 10) -> list[dict[str, Any]]:
    with get_conn(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT id, ts_utc, symbol, direction, quality_score, risk_decision,
                   entry, stop, target, rationale, created_at
            FROM signals
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _fetch_recent_trades(limit: int = 10) -> list[dict[str, Any]]:
    with get_conn(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT id, signal_id, symbol, direction, qty, entry_fill, stop_price, target_price,
                   exit_fill, pnl_dollars, status, opened_at, closed_at
            FROM trades
            ORDER BY opened_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def _fetch_recent_experiments(limit: int = 12) -> list[dict[str, Any]]:
    with get_conn(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT id, ts_utc, symbol, experiment_type, name, status, metrics_json,
                   parameters_json, artifacts_json, summary, recommendations_json
            FROM research_experiments
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                **dict(row),
                "metrics": _json_loads(row["metrics_json"], {}),
                "parameters": _json_loads(row["parameters_json"], {}),
                "artifacts": _json_loads(row["artifacts_json"], {}),
                "recommendations": _json_loads(row["recommendations_json"], []),
            }
            for row in rows
        ]


def _fetch_trading_stats() -> dict[str, Any]:
    phx = ZoneInfo("America/Phoenix")
    with get_conn(_db_path()) as conn:
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl_dollars ELSE 0 END), 0.0) AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_trades,
                COALESCE(SUM(CASE WHEN status = 'closed' AND pnl_dollars > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN status = 'closed' AND pnl_dollars <= 0 THEN 1 ELSE 0 END), 0) AS losses
            FROM trades
            """
        ).fetchone()
        today = conn.execute(
            """
            SELECT
                COUNT(*) AS trades_today,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl_dollars ELSE 0 END), 0.0) AS pnl_today
            FROM trades
            WHERE substr(opened_at, 1, 10) = strftime('%Y-%m-%d','now','localtime')
            """
        ).fetchone()
        by_symbol_rows = conn.execute(
            """
            SELECT
                symbol,
                COUNT(*) AS trade_count,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl_dollars ELSE 0 END), 0.0) AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_trades
            FROM trades
            GROUP BY symbol
            ORDER BY realized_pnl DESC, trade_count DESC
            """
        ).fetchall()
        by_strategy_rows = conn.execute(
            """
            SELECT
                COALESCE(strategy_family, 'unknown') AS strategy_family,
                COALESCE(strategy_version, 0) AS strategy_version,
                COUNT(*) AS trade_count,
                COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl_dollars ELSE 0 END), 0.0) AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_trades
            FROM trades
            GROUP BY strategy_family, strategy_version
            ORDER BY realized_pnl DESC, trade_count DESC
            """
        ).fetchall()
        signal_quality = conn.execute(
            """
            SELECT
                COUNT(*) AS signal_count,
                COALESCE(AVG(quality_score), 0.0) AS avg_quality
            FROM signals
            """
        ).fetchone()
        signal_decisions = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN risk_decision = 'TRADE' THEN 1 ELSE 0 END), 0) AS approved,
                COALESCE(SUM(CASE WHEN risk_decision = 'SKIP' THEN 1 ELSE 0 END), 0) AS skipped
            FROM signals
            """
        ).fetchone()

    wins = int(totals["wins"] or 0)
    losses = int(totals["losses"] or 0)
    closed = wins + losses
    return {
        "headline": {
            "total_trades": int(totals["total_trades"] or 0),
            "realized_pnl": round(float(totals["realized_pnl"] or 0.0), 2),
            "open_trades": int(totals["open_trades"] or 0),
            "win_rate": round(wins / closed, 3) if closed else 0.0,
            "closed_trades": closed,
            "trades_today": int(today["trades_today"] or 0),
            "pnl_today": round(float(today["pnl_today"] or 0.0), 2),
            "signal_count": int(signal_quality["signal_count"] or 0),
            "avg_signal_quality": round(float(signal_quality["avg_quality"] or 0.0), 2),
            "approved_signals": int(signal_decisions["approved"] or 0),
            "skipped_signals": int(signal_decisions["skipped"] or 0),
        },
        "by_symbol": [dict(row) for row in by_symbol_rows],
        "by_strategy": [dict(row) for row in by_strategy_rows],
        "generated_at_phx": datetime.now(timezone.utc).astimezone(phx).isoformat(),
    }


def _fetch_research_stats() -> dict[str, Any]:
    active = list_active_strategies()
    experiments = _fetch_recent_experiments(limit=50)
    walk_forwards = [exp for exp in experiments if exp.get("experiment_type") == "walk_forward"]
    sweeps = [exp for exp in experiments if exp.get("experiment_type") == "judas_threshold_sweep"]
    active_sorted = sorted(
        active,
        key=lambda row: float((row.get("metrics") or {}).get("profit_factor", 0.0)),
        reverse=True,
    )
    best_active = []
    for row in active_sorted[:12]:
        metrics = row.get("metrics") or {}
        best_active.append(
            {
                "symbol": row.get("symbol"),
                "strategy_name": (row.get("params") or {}).get("strategy_name"),
                "engine": (row.get("params") or {}).get("execution_engine"),
                "profit_factor": metrics.get("profit_factor"),
                "trades": metrics.get("trades"),
                "winrate": metrics.get("winrate"),
                "total_pnl_dollars": metrics.get("total_pnl_dollars"),
                "max_drawdown_dollars": metrics.get("max_drawdown_dollars"),
            }
        )

    recent_walk = []
    for exp in walk_forwards[:10]:
        metrics = exp.get("metrics") or {}
        recent_walk.append(
            {
                "id": exp.get("id"),
                "symbol": exp.get("symbol"),
                "name": exp.get("name"),
                "window_count": metrics.get("window_count"),
                "avg_test_profit_factor": metrics.get("avg_test_profit_factor"),
                "avg_test_expectancy_r": metrics.get("avg_test_expectancy_r"),
                "avg_test_max_drawdown_dollars": metrics.get("avg_test_max_drawdown_dollars"),
                "total_test_trades": metrics.get("total_test_trades"),
                "robustness_score": metrics.get("robustness_score"),
            }
        )

    return {
        "headline": {
            "active_strategy_count": len(active),
            "research_experiment_count": len(experiments),
            "walk_forward_count": len(walk_forwards),
            "sweep_count": len(sweeps),
        },
        "active_strategies": best_active,
        "recent_walk_forward": recent_walk,
        "recent_experiments": experiments[:12],
    }


def _service_snapshot() -> dict[str, str]:
    services = [
        "judas-dashboard.service",
        "judas-crew.service",
        "judas-crew.timer",
        "judas-research.service",
        "judas-research.timer",
    ]
    snapshot: dict[str, str] = {}
    for unit in services:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=ActiveState,SubState,Result", "--no-pager"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        summary = " | ".join(line.strip() for line in proc.stdout.splitlines() if line.strip())
        snapshot[unit] = summary or proc.stderr.strip() or "unknown"
    return snapshot


def _overview_payload() -> dict[str, Any]:
    session = json.loads(session_status_tool.run(input_json="{}"))
    signals = _fetch_recent_signals(limit=8)
    trades = _fetch_recent_trades(limit=8)
    experiments = _fetch_recent_experiments(limit=8)
    runtime_path = REPO_ROOT / "outputs" / "research" / "runtime_status.json"
    research_runtime = _json_loads(runtime_path.read_text(), {}) if runtime_path.exists() else {}
    now_utc = datetime.now(timezone.utc)
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    now_phx = now_utc.astimezone(ZoneInfo("America/Phoenix"))
    return {
        "now_utc": now_utc.isoformat(),
        "now_et": now_et.isoformat(),
        "now_phx": now_phx.isoformat(),
        "session": session,
        "services": _service_snapshot(),
        "research_runtime": research_runtime,
        "counts": {
            "signals": len(signals),
            "trades": len(trades),
            "experiments": len(experiments),
            "open_trades": sum(1 for trade in trades if trade.get("status") == "open"),
        },
        "latest_signal": signals[0] if signals else None,
        "latest_trade": trades[0] if trades else None,
        "latest_experiment": experiments[0] if experiments else None,
        "trading_stats": _fetch_trading_stats(),
        "research_stats": _fetch_research_stats(),
    }


def _build_chat_prompt(message: str) -> str:
    overview = _overview_payload()
    latest_signals = _fetch_recent_signals(limit=5)
    latest_trades = _fetch_recent_trades(limit=5)
    latest_experiments = _fetch_recent_experiments(limit=5)
    history = CHAT_HISTORY[-10:]
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history)
    return (
        f"{OPERATOR_MANAGER_PROMPT}\n\n"
        "You are replying inside the Judas dashboard. Be concise, concrete, and operational.\n"
        "Never claim a live action happened unless it is present in the provided context.\n\n"
        "The operator is in America/Phoenix. When replying, use PHX time by default. "
        "The backend and DB may still use UTC internally. This repo is IBKR paper-only, not live.\n\n"
        f"Current overview:\n{json.dumps(overview, indent=2)}\n\n"
        f"Recent signals:\n{json.dumps(latest_signals, indent=2)}\n\n"
        f"Recent trades:\n{json.dumps(latest_trades, indent=2)}\n\n"
        f"Recent experiments:\n{json.dumps(latest_experiments, indent=2)}\n\n"
        f"Conversation so far:\n{history_text or '[none]'}\n\n"
        f"User message: {message}\n"
    )


def _run_subprocess(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, output[-12000:]


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(DASHBOARD_DIST), static_url_path="/")
    _db_path()

    @app.get("/api/overview")
    def overview() -> Any:
        return jsonify(_overview_payload())

    @app.get("/api/signals")
    def signals() -> Any:
        return jsonify({"signals": _fetch_recent_signals(limit=int(request.args.get("limit", 20)))})

    @app.get("/api/trades")
    def trades() -> Any:
        return jsonify({"trades": _fetch_recent_trades(limit=int(request.args.get("limit", 20)))})

    @app.get("/api/experiments")
    def experiments() -> Any:
        return jsonify({"experiments": _fetch_recent_experiments(limit=int(request.args.get("limit", 20)))})

    @app.post("/api/chat")
    def chat() -> Any:
        payload = request.get_json(force=True)
        message = str(payload.get("message", "")).strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        lower = message.lower()
        if any(text in lower for text in ["run doctor", "doctor now", "health check now"]):
            code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "main.py", "--doctor", "--symbol", "MGC"])
            response = f"Doctor run {'completed' if code == 0 else 'failed'}.\n\n{output}"
        elif any(text in lower for text in ["run research", "start research", "kick off research"]):
            code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "scripts/research_tick.py", "--symbol", "MGC", "--reason", "operator-manager"])
            response = f"Research tick {'completed' if code == 0 else 'failed'}.\n\n{output}"
        else:
            CHAT_HISTORY.append({"role": "user", "content": message})
            prompt = _build_chat_prompt(message)
            response = str(build_llm().call(prompt)).strip()
        CHAT_HISTORY.append({"role": "assistant", "content": response})
        del CHAT_HISTORY[:-12]
        return jsonify({"response": response, "history": CHAT_HISTORY})

    @app.post("/api/run/doctor")
    def run_doctor() -> Any:
        symbol = str((request.get_json(silent=True) or {}).get("symbol", "MGC")).upper()
        code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "main.py", "--doctor", "--symbol", symbol])
        return jsonify({"ok": code == 0, "output": output})

    @app.post("/api/run/research")
    def run_research() -> Any:
        symbol = str((request.get_json(silent=True) or {}).get("symbol", "MGC")).upper()
        code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "scripts/run_research.py", "--symbol", symbol, "--log-level", "INFO"])
        return jsonify({"ok": code == 0, "output": output})

    @app.get("/", defaults={"path": ""})
    @app.get("/<path:path>")
    def frontend(path: str) -> Any:
        if path and (DASHBOARD_DIST / path).exists():
            return send_from_directory(DASHBOARD_DIST, path)
        return send_from_directory(DASHBOARD_DIST, "index.html")

    return app


def main() -> int:
    app = create_app()
    port = int(os.environ.get("JUDAS_DASHBOARD_PORT", "5080"))
    host = os.environ.get("JUDAS_DASHBOARD_HOST", "127.0.0.1")
    serve(app, host=host, port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
