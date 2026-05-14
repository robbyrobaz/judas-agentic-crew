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
from src import strategy_registry
from src.strategy_registry import list_active_strategies, reactivate_demoted
from src.tools.session_tools import session_status_tool

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIST = REPO_ROOT / "dashboard" / "dist"
CHAT_HISTORY: list[dict[str, str]] = []

# Shared goals preamble so the chat's framing matches the Operator agent's.
# Single source of truth lives in operator_agent.
from src.research.operator_agent import GOALS_PREAMBLE as _GOALS_PREAMBLE  # noqa: E402

OPERATOR_MANAGER_PROMPT = (
    "You are the Operator Manager for the entire Judas system. You oversee the agentic team "
    "(Operator + Researcher + Trader + Registrar + Coder) plus the live paper TradingCrew "
    "and the ResearchCrew. You have broad read access to current system state, timers, recent "
    "experiments, session status, findings memory, delegations, candidates, and demotions. "
    "You do not bypass deterministic trading gates, do not imply live trading, and do not "
    "pretend unverified actions occurred. You help the operator understand what is running, "
    "what failed, what matters next, and when to act. Your goals are the team's goals:\n\n"
    + _GOALS_PREAMBLE
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


def _format_scalar(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)):
        if abs(float(value)) >= 1000:
            return f"{float(value):,.2f}".rstrip("0").rstrip(".")
        return f"{float(value):,.3f}".rstrip("0").rstrip(".")
    return str(value)


_BRIEF_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS brief_action_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_date TEXT NOT NULL,
    action_index INTEGER NOT NULL,
    decision TEXT NOT NULL,
    action_json TEXT,
    result TEXT,
    decided_at_utc TEXT NOT NULL,
    UNIQUE(brief_date, action_index)
)
"""


def _ensure_brief_decisions_table(conn) -> None:
    conn.execute(_BRIEF_DECISIONS_DDL)


def _fetch_brief_decisions(brief_date: str) -> list[dict[str, Any]]:
    with get_conn(_db_path()) as conn:
        _ensure_brief_decisions_table(conn)
        rows = conn.execute(
            """
            SELECT action_index, decision, action_json, result, decided_at_utc
            FROM brief_action_decisions
            WHERE brief_date = ?
            ORDER BY action_index ASC
            """,
            (brief_date,),
        ).fetchall()
    return [
        {
            "action_index": int(row["action_index"]),
            "decision": str(row["decision"]),
            "action": _json_loads(row["action_json"], {}),
            "result": row["result"],
            "decided_at_utc": str(row["decided_at_utc"]),
        }
        for row in rows
    ]


def _decide_recommended(brief_date: str, action_index: int, *, decision: str) -> Any:
    """Apply or reject a recommended action by index. Idempotent per (brief_date, action_index)."""
    with get_conn(_db_path()) as conn:
        try:
            row = conn.execute(
                """
                SELECT summary_json FROM daily_briefs
                WHERE brief_date = ?
                ORDER BY created_at_utc DESC
                LIMIT 1
                """,
                (brief_date,),
            ).fetchone()
        except Exception:
            return jsonify({"ok": False, "error": "daily_briefs not initialized"}), 404
        if not row:
            return jsonify({"ok": False, "error": "brief not found"}), 404
        summary = _json_loads(row["summary_json"], {})
        actions = summary.get("recommended_actions") or []
        if action_index < 0 or action_index >= len(actions):
            return jsonify({"ok": False, "error": "action_index out of range"}), 400
        action = actions[action_index]

        _ensure_brief_decisions_table(conn)
        existing = conn.execute(
            """
            SELECT decision, result FROM brief_action_decisions
            WHERE brief_date = ? AND action_index = ?
            """,
            (brief_date, action_index),
        ).fetchone()
        if existing:
            return jsonify({
                "ok": True,
                "action": action,
                "result": existing["result"],
                "decision": existing["decision"],
                "already_decided": True,
            })

    result_text = "no-op"
    if decision == "apply":
        atype = str(action.get("type") or "").lower()
        if atype == "retire":
            demotion_id = action.get("demotion_id")
            sid = action.get("strategy_id")
            if demotion_id is not None:
                result_text = f"already retired (demotion_id={demotion_id})"
            elif sid is not None:
                try:
                    new_demotion = strategy_registry.retire_strategy(
                        strategy_id=int(sid),
                        reason=str(action.get("reason") or "operator-applied"),
                        metrics_snapshot={"source": "daily_brief", "brief_date": brief_date},
                    )
                    result_text = f"retired strategy {sid}; demotion_id={new_demotion}"
                except Exception as exc:
                    return jsonify({"ok": False, "error": str(exc)}), 400
            else:
                return jsonify({"ok": False, "error": "missing strategy_id"}), 400
        elif atype == "review":
            result_text = "review acknowledged"
        else:
            result_text = f"unknown action type: {atype}"
    else:
        result_text = "rejected"

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn(_db_path()) as conn:
        _ensure_brief_decisions_table(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO brief_action_decisions
                (brief_date, action_index, decision, action_json, result, decided_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (brief_date, action_index, decision, json.dumps(action), result_text, now),
        )
        conn.commit()
    return jsonify({"ok": True, "action": action, "result": result_text, "decision": decision})


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


def _fetch_chat_active_strategies(limit: int = 30) -> list[dict[str, Any]]:
    """Compact view of currently-active strategies for the chat prompt."""
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, strategy_family, version, activated_at_utc,
                       substr(notes, 1, 120) AS notes
                FROM active_strategies
                WHERE state = 'active'
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _fetch_chat_open_delegations(limit: int = 15) -> list[dict[str, Any]]:
    """Open / in-flight agent_tasks the team is working on."""
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, requested_at_utc, requester, team, action, urgency,
                       status, claimed_by, substr(rationale, 1, 200) AS rationale
                FROM agent_tasks
                WHERE status IN ('open', 'claimed')
                ORDER BY
                    CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                    requested_at_utc DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _fetch_chat_recent_demotions(limit: int = 5) -> list[dict[str, Any]]:
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, ts_utc, symbol, strategy_family, version,
                       substr(reason, 1, 200) AS reason, reactivated_at_utc
                FROM auto_demotions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _fetch_chat_pending_candidates(limit: int = 5) -> list[dict[str, Any]]:
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, ts_utc, symbol, strategy_family, decision,
                       substr(rationale, 1, 200) AS rationale, status
                FROM strategy_candidates
                WHERE status = 'candidate'
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _fetch_chat_recent_findings(limit: int = 10) -> list[dict[str, Any]]:
    """Team's shared memory — backed by CrewAI Memory since 2026-05-11.
    Composite scoring surfaces importance-weighted memories ahead of
    raw recency."""
    try:
        from src.research import memory_backend
        return memory_backend.search_memory(query=None, limit=limit)
    except Exception:
        return []


def _fetch_chat_recent_brief_meta(limit: int = 2) -> list[dict[str, Any]]:
    """Brief metadata (no body — body lives in the JSON summary which we keep)."""
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, brief_date, created_at_utc, summary_json,
                       acknowledged_at_utc
                FROM daily_briefs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["summary"] = _json_loads(d.pop("summary_json", "{}"), {})
            out.append(d)
        return out
    except Exception:
        return []


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


_TICK_VALUE: dict[str, float] = {
    "MGC": 1.0, "MNQ": 0.5, "MCL": 1.0, "MBT": 0.5,
    "MET": 0.05, "DX": 5.0, "ZF": 7.8125, "6J": 6.25, "ZN": 15.625,
}
_TICK_SIZE: dict[str, float] = {
    "MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "MBT": 5.0,
    "MET": 0.50, "DX": 0.005, "ZF": 0.0078125, "6J": 0.0000005, "ZN": 0.015625,
}


def _dollar_per_point(symbol: str) -> float:
    ts = _TICK_SIZE.get(symbol.upper(), 0.01)
    tv = _TICK_VALUE.get(symbol.upper(), 1.0)
    return tv / ts if ts else 1.0


def _load_price_cache() -> dict[str, dict]:
    """Load last_bar_closes.json, then fill any gaps from cached parquet files."""
    p = REPO_ROOT / "last_bar_closes.json"
    cache: dict[str, dict] = {}
    if p.exists():
        try:
            cache = json.loads(p.read_text())
        except Exception:
            pass
    # Supplement with parquet files for symbols not in JSON cache.
    cache_dir = REPO_ROOT / "cache_1h"
    if cache_dir.exists():
        try:
            import pandas as pd
            for parquet in cache_dir.glob("*_1h.parquet"):
                sym = parquet.stem.replace("_1h", "").upper()
                if sym in cache:
                    continue
                try:
                    df = pd.read_parquet(parquet, columns=["ts", "close"])
                    if df.empty:
                        continue
                    last = df.iloc[-1]
                    cache[sym] = {"close": float(last["close"]), "ts": str(last["ts"])}
                except Exception:
                    pass
        except Exception:
            pass
    return cache


def _fetch_open_positions() -> list[dict[str, Any]]:
    """Return open trades enriched with current price and unrealized P&L."""
    price_cache = _load_price_cache()
    try:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.symbol, t.direction, t.qty,
                       t.entry_fill, t.stop_price, t.target_price,
                       t.opened_at,
                       a.strategy_family, a.version
                FROM trades t
                LEFT JOIN active_strategies a ON a.id = (
                    SELECT CAST(value AS INTEGER)
                    FROM json_each(COALESCE(t.meta_json, '{}'))
                    WHERE key = 'active_strategy_id'
                    LIMIT 1
                )
                WHERE t.status = 'open'
                ORDER BY t.opened_at DESC
                """,
            ).fetchall()
    except Exception:
        rows = []
        try:
            with get_conn(_db_path()) as conn:
                rows = conn.execute(
                    "SELECT id, symbol, direction, qty, entry_fill, stop_price, target_price, opened_at FROM trades WHERE status='open' ORDER BY opened_at DESC"
                ).fetchall()
        except Exception:
            pass

    result = []
    for row in rows:
        sym = str(row["symbol"]).upper()
        dpp = _dollar_per_point(sym)
        entry = float(row["entry_fill"] or 0)
        stop = float(row["stop_price"] or 0)
        target = float(row["target_price"] or 0)
        qty = int(row["qty"] or 1)
        direction = str(row["direction"])

        risk_pts = abs(entry - stop)
        reward_pts = abs(target - entry)
        risk_dollars = -round(risk_pts * dpp * qty, 2)
        reward_dollars = round(reward_pts * dpp * qty, 2)
        rr = round(reward_pts / risk_pts, 2) if risk_pts else None

        pc = price_cache.get(sym) or price_cache.get(sym.lower())
        current_price = float(pc["close"]) if pc and "close" in pc else None
        price_ts = str(pc["ts"]) if pc and "ts" in pc else None

        unrealized_pnl = None
        if current_price is not None and entry:
            diff = (current_price - entry) if direction == "long" else (entry - current_price)
            unrealized_pnl = round(diff * dpp * qty, 2)

        fam = row["strategy_family"] if "strategy_family" in row.keys() else None
        ver = row["version"] if "version" in row.keys() else None
        label = f"{fam} v{ver}" if fam and ver else None

        result.append({
            "id": int(row["id"]),
            "symbol": sym,
            "direction": direction,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "risk_dollars": risk_dollars,
            "reward_dollars": reward_dollars,
            "rr": rr,
            "current_price": current_price,
            "price_ts": price_ts,
            "unrealized_pnl": unrealized_pnl,
            "opened_at": str(row["opened_at"]),
            "label": label,
        })
    return result


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
        "open_positions": _fetch_open_positions(),
    }


def _build_chat_prompt(message: str) -> str:
    overview = _overview_payload()
    latest_signals = _fetch_recent_signals(limit=5)
    latest_trades = _fetch_recent_trades(limit=5)
    latest_experiments = _fetch_recent_experiments(limit=8)
    # Phase 10/11 context — make the chat see what the agentic team sees.
    active_strategies = _fetch_chat_active_strategies(limit=30)
    open_delegations = _fetch_chat_open_delegations(limit=15)
    recent_demotions = _fetch_chat_recent_demotions(limit=5)
    pending_candidates = _fetch_chat_pending_candidates(limit=5)
    recent_findings = _fetch_chat_recent_findings(limit=10)
    recent_briefs_meta = _fetch_chat_recent_brief_meta(limit=2)
    # Truncate history aggressively. Long history was causing the model to
    # reference experiment ids it discussed days ago and ignore the
    # freshly-pulled rows directly above it. Keep only the immediate context.
    history = CHAT_HISTORY[-4:]
    history_text = "\n".join(f"{item['role']}: {item['content']}" for item in history)

    # Loud freshness banner. Compute a one-line snapshot the model can't
    # plausibly miss when scanning the prompt.
    if latest_experiments:
        latest_exp = latest_experiments[0]
        ladder = ", ".join(
            f"#{e['id']} {e.get('experiment_type','')}@{e.get('ts_utc','')[:19]}"
            for e in latest_experiments[:5]
        )
        freshness = (
            f"FRESHNESS BANNER (this overrides anything you said earlier in chat history):\n"
            f"  - Latest research_experiments row id is #{latest_exp['id']} "
            f"({latest_exp.get('experiment_type','')}, {latest_exp.get('name','')}), "
            f"ts {latest_exp.get('ts_utc','')}.\n"
            f"  - Last 5 experiment ids/types: {ladder}.\n"
            f"  - Total recent experiments returned: {len(latest_experiments)}.\n"
            f"If your reply names an experiment id older than #{latest_exp['id']} as 'the latest', "
            f"you are wrong — re-read the Recent experiments block below and use that.\n"
        )
    else:
        freshness = "FRESHNESS BANNER: no research_experiments rows present.\n"

    return (
        f"{OPERATOR_MANAGER_PROMPT}\n\n"
        "You are replying inside the Judas dashboard. Be concise, concrete, and operational. "
        "You speak with the same context the agentic team sees — Operator + Researcher + "
        "Trader + Registrar + Coder. The team works through delegations and shared findings; "
        "the blocks below are exactly what an agent would consult.\n"
        "Never claim a live action happened unless it is present in the provided context.\n"
        "Always treat the provided blocks as ground truth. If chat history references stale "
        "ids that conflict with the fresh data, the fresh data wins.\n\n"
        "The operator is in America/Phoenix. When replying, use PHX time by default. "
        "The backend and DB may still use UTC internally. This repo is IBKR paper-only, not live.\n\n"
        f"{freshness}\n"
        f"Current overview:\n{json.dumps(overview, indent=2)}\n\n"
        f"Active strategies ({len(active_strategies)}):\n{json.dumps(active_strategies, indent=2)}\n\n"
        f"Open delegations (agent_tasks — what's queued for the specialists):\n"
        f"{json.dumps(open_delegations, indent=2)}\n\n"
        f"Recent demotions:\n{json.dumps(recent_demotions, indent=2)}\n\n"
        f"Pending candidates (queue waiting for promotion):\n"
        f"{json.dumps(pending_candidates, indent=2)}\n\n"
        f"Team findings (shared agent memory — what the team has learned):\n"
        f"{json.dumps(recent_findings, indent=2)}\n\n"
        f"Recent daily briefs (meta only — body excluded for token budget):\n"
        f"{json.dumps(recent_briefs_meta, indent=2)}\n\n"
        f"Recent signals:\n{json.dumps(latest_signals, indent=2)}\n\n"
        f"Recent trades:\n{json.dumps(latest_trades, indent=2)}\n\n"
        f"Recent experiments:\n{json.dumps(latest_experiments, indent=2)}\n\n"
        f"Conversation so far (older context, may be stale — fresh blocks above win):\n"
        f"{history_text or '[none]'}\n\n"
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


def _extract_last_json_block(output: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for index in range(len(lines) - 1, -1, -1):
        candidate = "\n".join(lines[index:])
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _format_doctor_output(symbol: str, ok: bool, output: str) -> str:
    payload = _extract_last_json_block(output) or {}
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    llm = checks.get("llm") if isinstance(checks.get("llm"), dict) else {}
    ibkr = checks.get("ibkr_data") if isinstance(checks.get("ibkr_data"), dict) else {}
    session = checks.get("session_status") if isinstance(checks.get("session_status"), dict) else {}
    status = "passed" if ok else "failed"
    lines = [
        f"### Doctor {status}",
        f"- Symbol: `{symbol}`",
        f"- LLM: {'OK' if llm.get('ok') else 'FAIL'}",
        f"- IBKR data: {'OK' if ibkr.get('ok') else 'FAIL'}",
        f"- Session: {'tradeable' if session.get('can_trade_now') else 'closed'}",
    ]
    if ibkr:
        lines.append(
            f"- Bars: `{_format_scalar(ibkr.get('bar_count'))}` | Prior high: `{_format_scalar(ibkr.get('prior_high'))}` | Prior low: `{_format_scalar(ibkr.get('prior_low'))}`"
        )
    if session:
        lines.append(f"- Session note: {session.get('reason', '—')}")
    if not payload:
        tail = output.strip().splitlines()[-8:]
        if tail:
            lines.extend(["", "```text", *tail, "```"])
    return "\n".join(lines)


def _format_research_output(symbol: str, ok: bool, output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    status = "started" if ok else "failed"
    summary = [
        f"### Research {status}",
        f"- Symbol: `{symbol}`",
    ]
    tail = lines[-6:]
    if tail:
        summary.extend(["", "```text", *tail, "```"])
    return "\n".join(summary)


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
            symbol = "MGC"
            code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "main.py", "--doctor", "--symbol", symbol])
            response = _format_doctor_output(symbol, code == 0, output)
        elif any(text in lower for text in ["run research", "start research", "kick off research"]):
            symbol = "MGC"
            code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "scripts/research_tick.py", "--symbol", symbol, "--reason", "operator-manager"])
            response = _format_research_output(symbol, code == 0, output)
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
        return jsonify({"ok": code == 0, "output": _format_doctor_output(symbol, code == 0, output), "raw_output": output})

    @app.post("/api/run/research")
    def run_research() -> Any:
        symbol = str((request.get_json(silent=True) or {}).get("symbol", "MGC")).upper()
        code, output = _run_subprocess([str(REPO_ROOT / ".venv" / "bin" / "python"), "scripts/run_research.py", "--symbol", symbol, "--log-level", "INFO"])
        return jsonify({"ok": code == 0, "output": _format_research_output(symbol, code == 0, output), "raw_output": output})

    @app.post("/api/run/researcher")
    def run_researcher() -> Any:
        code, output = _run_subprocess([
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "scripts/run_researcher.py",
        ])
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        status = "completed" if code == 0 else "failed"
        tail = lines[-8:]
        body = "\n".join(["### Researcher " + status, "", "```text"] + tail + ["```"])
        return jsonify({"ok": code == 0, "output": body, "raw_output": output})

    @app.post("/api/run/youtube")
    def run_youtube() -> Any:
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query", "ICT liquidity sweep 2026")).strip()
        # Inject a researcher task via the DB directly so the next researcher run picks it up
        with get_conn(_db_path()) as conn:
            conn.execute(
                """INSERT INTO agent_tasks
                   (requested_at_utc, requester, team, action, payload_json, rationale, urgency, status)
                   VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'dashboard', 'researcher',
                           'youtube_search', ?, 'Dashboard YouTube search request', 'high', 'open')""",
                (json.dumps({"query": query}),),
            )
        # Then immediately trigger a researcher run
        code, output = _run_subprocess([
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "scripts/run_researcher.py",
        ])
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        status = "completed" if code == 0 else "failed"
        tail = lines[-8:]
        body = "\n".join([f"### YouTube search '{query}' {status}", "", "```text"] + tail + ["```"])
        return jsonify({"ok": code == 0, "output": body, "query": query})

    @app.post("/api/run/sweep")
    def run_sweep() -> Any:
        payload = request.get_json(silent=True) or {}
        strategy_family = str(payload.get("strategy_family", "judas_1h"))
        params = payload.get("params", {})
        # Inject a researcher task to sweep all 8 symbols
        task_payload = {
            "strategy_family": strategy_family,
            "params": params,
            "symbols": ["MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"],
        }
        with get_conn(_db_path()) as conn:
            conn.execute(
                """INSERT INTO agent_tasks
                   (requested_at_utc, requester, team, action, payload_json, rationale, urgency, status)
                   VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'dashboard', 'researcher',
                           'sweep_all_symbols', ?, 'Dashboard sweep all symbols request', 'high', 'open')""",
                (json.dumps(task_payload),),
            )
        # Trigger researcher run
        code, output = _run_subprocess([
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "scripts/run_researcher.py",
        ])
        lines = [l.strip() for l in output.splitlines() if l.strip()]
        status = "completed" if code == 0 else "failed"
        tail = lines[-8:]
        body = "\n".join([f"### Sweep all symbols ({strategy_family}) {status}", "", "```text"] + tail + ["```"])
        return jsonify({"ok": code == 0, "output": body})

    @app.get("/api/demotions")
    def demotions() -> Any:
        with get_conn(_db_path()) as conn:
            rows = conn.execute(
                """
                SELECT id, ts_utc, strategy_id, symbol, strategy_family, version,
                       reason, reactivated_at_utc, reactivated_strategy_id
                FROM auto_demotions
                WHERE ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')
                ORDER BY ts_utc DESC
                """
            ).fetchall()
        return jsonify({
            "demotions": [
                {
                    "id": int(row["id"]),
                    "ts_utc": str(row["ts_utc"]),
                    "strategy_id": int(row["strategy_id"]),
                    "symbol": str(row["symbol"]),
                    "strategy_family": str(row["strategy_family"]),
                    "version": int(row["version"]),
                    "reason": str(row["reason"]),
                    "reactivated": row["reactivated_at_utc"] is not None,
                    "reactivated_at_utc": row["reactivated_at_utc"],
                    "reactivated_strategy_id": row["reactivated_strategy_id"],
                }
                for row in rows
            ]
        })

    @app.post("/api/demotions/<int:demotion_id>/reactivate")
    def reactivate(demotion_id: int) -> Any:
        try:
            new_id = reactivate_demoted(demotion_id=demotion_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "new_strategy_id": new_id})

    @app.get("/api/briefs")
    def list_briefs() -> Any:
        """List daily briefs from the last 30 days, most recent first."""
        with get_conn(_db_path()) as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT id, brief_date, created_at_utc, summary_json, acknowledged_at_utc
                    FROM daily_briefs
                    WHERE brief_date >= date('now', '-30 days')
                    ORDER BY brief_date DESC, created_at_utc DESC
                    """
                ).fetchall()
            except Exception:
                # Table may not exist until Worker E's migration lands.
                return jsonify({"briefs": []})
        return jsonify({
            "briefs": [
                {
                    "id": int(row["id"]),
                    "brief_date": str(row["brief_date"]),
                    "created_at_utc": str(row["created_at_utc"]),
                    "summary": _json_loads(row["summary_json"], {}),
                    "acknowledged_at_utc": row["acknowledged_at_utc"],
                }
                for row in rows
            ]
        })

    @app.get("/api/briefs/<brief_date>")
    def get_brief(brief_date: str) -> Any:
        with get_conn(_db_path()) as conn:
            try:
                row = conn.execute(
                    """
                    SELECT id, brief_date, created_at_utc, content_md, summary_json,
                           acknowledged_at_utc
                    FROM daily_briefs
                    WHERE brief_date = ?
                    ORDER BY created_at_utc DESC
                    LIMIT 1
                    """,
                    (brief_date,),
                ).fetchone()
            except Exception:
                return jsonify({"error": "daily_briefs not initialized"}), 404
        if not row:
            return jsonify({"error": f"no brief for {brief_date}"}), 404
        decisions = _fetch_brief_decisions(brief_date)
        return jsonify({
            "id": int(row["id"]),
            "brief_date": str(row["brief_date"]),
            "created_at_utc": str(row["created_at_utc"]),
            "content_md": row["content_md"] or "",
            "summary": _json_loads(row["summary_json"], {}),
            "acknowledged_at_utc": row["acknowledged_at_utc"],
            "decisions": decisions,
        })

    @app.post("/api/briefs/<brief_date>/ack")
    def ack_brief(brief_date: str) -> Any:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn(_db_path()) as conn:
            cur = conn.execute(
                """
                UPDATE daily_briefs
                SET acknowledged_at_utc = COALESCE(acknowledged_at_utc, ?)
                WHERE brief_date = ?
                """,
                (now, brief_date),
            )
            if cur.rowcount == 0:
                return jsonify({"ok": False, "error": "not found"}), 404
            conn.commit()
        return jsonify({"ok": True})

    @app.post("/api/briefs/<brief_date>/recommended/<int:action_index>/apply")
    def apply_recommended(brief_date: str, action_index: int) -> Any:
        return _decide_recommended(brief_date, action_index, decision="apply")

    @app.post("/api/briefs/<brief_date>/recommended/<int:action_index>/reject")
    def reject_recommended(brief_date: str, action_index: int) -> Any:
        return _decide_recommended(brief_date, action_index, decision="reject")

    @app.get("/api/autofixes")
    def list_autofixes() -> Any:
        status_filter = (request.args.get("status") or "open").lower()
        with get_conn(_db_path()) as conn:
            try:
                if status_filter == "all":
                    rows = conn.execute(
                        """
                        SELECT id, started_at_utc, finished_at_utc, symptom_category,
                               symptom_summary, branch_name, status, test_result,
                               pushed, operator_decision
                        FROM auto_fixes
                        ORDER BY started_at_utc DESC, id DESC
                        LIMIT 50
                        """
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, started_at_utc, finished_at_utc, symptom_category,
                               symptom_summary, branch_name, status, test_result,
                               pushed, operator_decision
                        FROM auto_fixes
                        WHERE operator_decision IS NULL
                        ORDER BY started_at_utc DESC, id DESC
                        LIMIT 50
                        """
                    ).fetchall()
            except Exception:
                return jsonify({"autofixes": []})
        return jsonify({
            "autofixes": [
                {
                    "id": int(row["id"]),
                    "started_at_utc": row["started_at_utc"],
                    "finished_at_utc": row["finished_at_utc"],
                    "symptom_category": row["symptom_category"],
                    "symptom_summary": row["symptom_summary"],
                    "branch_name": row["branch_name"],
                    "status": row["status"],
                    "test_result": row["test_result"],
                    "pushed": bool(row["pushed"]) if row["pushed"] is not None else False,
                    "operator_decision": row["operator_decision"],
                }
                for row in rows
            ]
        })

    @app.get("/api/autofixes/<int:autofix_id>")
    def get_autofix(autofix_id: int) -> Any:
        with get_conn(_db_path()) as conn:
            try:
                row = conn.execute(
                    """
                    SELECT id, started_at_utc, finished_at_utc, symptom_category,
                           symptom_hash, symptom_summary, branch_name, worktree_path,
                           prompt, diff_summary, files_changed_json, test_result,
                           test_output_tail, pushed, operator_decision,
                           operator_decision_at_utc, status
                    FROM auto_fixes
                    WHERE id = ?
                    """,
                    (autofix_id,),
                ).fetchone()
            except Exception:
                return jsonify({"error": "auto_fixes not initialized"}), 404
        if not row:
            return jsonify({"error": f"no autofix with id {autofix_id}"}), 404
        return jsonify({
            "id": int(row["id"]),
            "started_at_utc": row["started_at_utc"],
            "finished_at_utc": row["finished_at_utc"],
            "symptom_category": row["symptom_category"],
            "symptom_hash": row["symptom_hash"],
            "symptom_summary": row["symptom_summary"],
            "branch_name": row["branch_name"],
            "worktree_path": row["worktree_path"],
            "prompt": row["prompt"],
            "diff_summary": row["diff_summary"],
            "files_changed": _json_loads(row["files_changed_json"], []),
            "test_result": row["test_result"],
            "test_output_tail": row["test_output_tail"],
            "pushed": bool(row["pushed"]) if row["pushed"] is not None else False,
            "operator_decision": row["operator_decision"],
            "operator_decision_at_utc": row["operator_decision_at_utc"],
            "status": row["status"],
        })
    @app.post("/api/autofixes/<int:autofix_id>/merge")
    def autofix_merge(autofix_id: int) -> Any:
        """Merge a pushed autofix branch into master.

        Safety pattern (answers the half-merged question):
          1. ``pre_sha = git rev-parse master`` BEFORE any mutation.
          2. ``git merge --no-ff <branch>`` locally. On failure → ``git merge --abort``,
             return error. Master is untouched.
          3. ``git push origin master``. On failure → ``git reset --hard <pre_sha>``,
             return error. Local rolled back; origin never advanced.
          4. Only after a successful push do we update the DB row.
        """
        with get_conn(_db_path()) as conn:
            try:
                row = conn.execute(
                    "SELECT branch_name, pushed, operator_decision FROM auto_fixes WHERE id = ?",
                    (autofix_id,),
                ).fetchone()
            except Exception as exc:
                return jsonify({"ok": False, "error": f"auto_fixes not initialized: {exc}"}), 404
        if not row:
            return jsonify({"ok": False, "error": "autofix not found"}), 404
        if not int(row["pushed"] or 0):
            return jsonify({"ok": False, "error": "branch not pushed"}), 400
        if row["operator_decision"]:
            return jsonify({"ok": False, "error": f"already decided: {row['operator_decision']}"}), 400
        branch = str(row["branch_name"])

        repo = str(REPO_ROOT)

        def _git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)

        pre = _git("rev-parse", "master")
        if pre.returncode != 0:
            return jsonify({"ok": False, "error": f"could not resolve master: {pre.stderr.strip()}"}), 500
        pre_sha = pre.stdout.strip()

        # Make sure we are on master locally.
        co = _git("checkout", "master")
        if co.returncode != 0:
            return jsonify({"ok": False, "error": f"checkout master failed: {co.stderr.strip()}"}), 500

        merge = _git("merge", "--no-ff", "-m", f"Merge autofix branch {branch}", branch)
        if merge.returncode != 0:
            _git("merge", "--abort")
            # Belt-and-braces: ensure we are still at pre_sha.
            _git("reset", "--hard", pre_sha)
            return jsonify({
                "ok": False,
                "error": f"merge failed (master untouched): {merge.stderr.strip() or merge.stdout.strip()}",
            }), 409

        push = _git("push", "origin", "master")
        if push.returncode != 0:
            _git("reset", "--hard", pre_sha)
            return jsonify({
                "ok": False,
                "error": f"push failed; rolled back local master to {pre_sha[:8]}: {push.stderr.strip()}",
            }), 502

        post = _git("rev-parse", "master")
        master_sha = post.stdout.strip() if post.returncode == 0 else ""

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn(_db_path()) as conn:
            conn.execute(
                """
                UPDATE auto_fixes
                SET operator_decision = 'merged', operator_decision_at_utc = ?
                WHERE id = ?
                """,
                (now, autofix_id),
            )
            conn.commit()
        return jsonify({"ok": True, "master_sha": master_sha})

    @app.post("/api/autofixes/<int:autofix_id>/reject")
    def autofix_reject(autofix_id: int) -> Any:
        """Mark an autofix as rejected.

        We keep the remote branch by default for forensics (per design open
        question #3). Pass ``{"delete_branch": true}`` to also delete it.
        """
        body = request.get_json(silent=True) or {}
        delete_branch = bool(body.get("delete_branch", False))

        with get_conn(_db_path()) as conn:
            try:
                row = conn.execute(
                    "SELECT branch_name, pushed, operator_decision FROM auto_fixes WHERE id = ?",
                    (autofix_id,),
                ).fetchone()
            except Exception as exc:
                return jsonify({"ok": False, "error": f"auto_fixes not initialized: {exc}"}), 404
        if not row:
            return jsonify({"ok": False, "error": "autofix not found"}), 404
        if not int(row["pushed"] or 0):
            return jsonify({"ok": False, "error": "branch not pushed"}), 400
        if row["operator_decision"]:
            return jsonify({"ok": False, "error": f"already decided: {row['operator_decision']}"}), 400

        branch = str(row["branch_name"])
        if delete_branch:
            subprocess.run(
                ["git", "-C", str(REPO_ROOT), "push", "origin", "--delete", branch],
                capture_output=True,
                text=True,
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_conn(_db_path()) as conn:
            conn.execute(
                """
                UPDATE auto_fixes
                SET operator_decision = 'rejected', operator_decision_at_utc = ?
                WHERE id = ?
                """,
                (now, autofix_id),
            )
            conn.commit()
        return jsonify({"ok": True})

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
