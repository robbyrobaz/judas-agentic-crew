"""PM agent — loose-mandate elite-trader agent loop.

Per the Agentic Operator Plan: a single agent with the full mandate to make
money, equipped with a tool palette that wraps the existing code-enforced
deterministic paths (registry, broker, research). Hard safety stays in code:
this module never opens raw IBKR orders, never executes arbitrary shell or
Python, and never writes raw SQL to the database — every action goes through
an atomic helper that already enforces audit trails and paper-only mode.

Public surface:

    PMAction        — one tool invocation + its result
    PMDecisionResult — full outcome of a PM cycle
    run_pm_decision — entry point used by OperatorFlow.morning_review

Tests exercise this module by monkeypatching ``_call_llm`` (the litellm seam)
and ``_place_bracket`` (the broker seam). They never reach the network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET_TZ = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Loose mandate — verbatim. Tests assert this is NOT re-narrowed with phrases
# like "validate before" or "every drop must" or "rolling waiting room".
# ---------------------------------------------------------------------------

PM_SYSTEM_PROMPT = """\
You are an elite futures trader with a $5,000 paper IBKR account.
Your only job is to make as much money as possible.

You have full access to all tools, live data, backtesting, the strategy
registry, and the ability to modify strategies, retire them, promote
new ones, and place your own paper trades when you see high-conviction
setups.

Think like a profit-maximizing PM. Be decisive. Use tools aggressively.
Backtest when uncertain. Retire anything not contributing. Keep evolving
the system. The active set is not a portfolio to protect — it's a
rolling list of bets you've decided are worth running today.

You have {turn_budget} tool calls and {time_budget_s} seconds. End by
emitting a brief summary of what you did and why.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PMAction:
    action: str
    target_id: int | None
    payload: dict
    rationale: str
    tool_result: dict


@dataclass
class PMDecisionResult:
    success: bool
    actions_taken: list[PMAction]
    narrative: str
    turns_used: int
    elapsed_s: float
    fallback_used: bool
    raw_messages: list[dict] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Module-level seams — tests monkeypatch these
# ---------------------------------------------------------------------------


def _place_bracket(**kwargs) -> dict[str, Any]:
    """Broker seam — defers to portfolio_runtime.place_bracket.

    Tests monkeypatch this attribute on the module.
    """
    from src.portfolio_runtime import place_bracket as _impl

    return _impl(**kwargs)


# Tests can flip this to True to simulate dry-run paper mode.
_BROKER_DRY_RUN = False


_VALID_SYMBOLS = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}


_OUTPUT_ROW_CAP = 200
_OUTPUT_BYTES_CAP = 16 * 1024


# ---------------------------------------------------------------------------
# query_db safety — strip comments + leading whitespace, allow only one SELECT
# ---------------------------------------------------------------------------


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_noise(sql: str) -> str:
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    return sql.strip()


def _is_safe_select(sql: str) -> tuple[bool, str | None]:
    if not isinstance(sql, str) or not sql.strip():
        return False, "empty sql"
    cleaned = _strip_sql_noise(sql)
    if not cleaned:
        return False, "empty after stripping comments"
    head = cleaned[:6].upper()
    if head != "SELECT":
        return False, "only SELECT statements are allowed"
    # Reject multi-statements: any non-whitespace / non-comment after a `;`
    if ";" in cleaned:
        # Permit only a trailing `;` with nothing meaningful after it.
        idx = cleaned.find(";")
        tail = _strip_sql_noise(cleaned[idx + 1 :])
        if tail:
            return False, "multi-statement queries are not allowed"
    # Reject pragmas + attaches even though SELECT-prefix would already block.
    lowered = cleaned.lower()
    for forbidden in ("pragma", "attach", "detach"):
        if re.search(rf"\b{forbidden}\b", lowered):
            return False, f"{forbidden} not allowed"
    return True, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Tool palette factory
# ---------------------------------------------------------------------------


def _safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool implementation so exceptions become {ok: false, error: ...}."""

    def wrapped(**kwargs) -> Any:
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 — defensive on the agent seam
            log.exception("pm_agent.tool.failed", extra={"tool": fn.__name__})
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    wrapped.__name__ = fn.__name__
    return wrapped


def _make_tools(*, db_path: str) -> dict[str, Callable[..., Any]]:
    """Build the tool palette bound to ``db_path``."""

    # ---- read tools ------------------------------------------------------

    def get_active_strategies() -> list[dict]:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, symbol, strategy_family, version, params_json, "
                "metrics_json, activated_at_utc, state "
                "FROM active_strategies WHERE state = 'active' ORDER BY id"
            ).fetchall()
        out: list[dict] = []
        now = datetime.now(timezone.utc)
        for r in rows:
            try:
                params = json.loads(r["params_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                params = {}
            try:
                metrics = json.loads(r["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            sid = int(r["id"])
            # Derive trade-level rolling metrics.
            with _connect(db_path) as conn2:
                tcount = conn2.execute(
                    "SELECT COUNT(*) AS n FROM trades WHERE strategy_id = ? AND status = 'closed'",
                    (sid,),
                ).fetchone()
                tsum = conn2.execute(
                    "SELECT COALESCE(SUM(pnl_dollars),0) AS s FROM trades WHERE strategy_id = ? AND status = 'closed'",
                    (sid,),
                ).fetchone()
                last = conn2.execute(
                    "SELECT MAX(opened_at) AS m FROM trades WHERE strategy_id = ?",
                    (sid,),
                ).fetchone()
            try:
                from src.research.live_review import compute_live_metrics

                lm = compute_live_metrics(db_path=db_path, strategy_id=sid)
                pf_20 = lm.pf_20
                expectancy_20 = lm.expectancy_20
                days_since_last_fire = lm.days_since_last_fire
            except Exception:  # noqa: BLE001
                pf_20 = None
                expectancy_20 = None
                days_since_last_fire = None
            try:
                act = datetime.fromisoformat(
                    str(r["activated_at_utc"]).replace("Z", "+00:00")
                )
                if act.tzinfo is None:
                    act = act.replace(tzinfo=timezone.utc)
                activated_days_ago = max(0, (now - act).days)
            except Exception:  # noqa: BLE001
                activated_days_ago = None
            out.append(
                {
                    "id": sid,
                    "symbol": str(r["symbol"]),
                    "strategy_family": str(r["strategy_family"]),
                    "version": int(r["version"]),
                    "strategy_name": params.get("strategy_name", ""),
                    "n_closed_trades": int(tcount["n"]),
                    "total_pnl": float(tsum["s"] or 0.0),
                    "pf_20": pf_20,
                    "expectancy_20": expectancy_20,
                    "days_since_last_fire": days_since_last_fire,
                    "activated_days_ago": activated_days_ago,
                }
            )
        return out

    def get_strategy_detail(*, id: int) -> dict:
        sid = int(id)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ?", (sid,)
            ).fetchone()
        if row is None:
            return {"ok": False, "error": f"strategy {sid} not found"}
        d = _row_to_dict(row)
        try:
            d["params"] = json.loads(d.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["params"] = {}
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["metrics"] = {}
        return {"ok": True, "strategy": d}

    def get_workshop_leaderboard(*, limit: int = 20) -> list[dict]:
        from src.research.explore import _load_workshop_leaderboard_compact

        return _load_workshop_leaderboard_compact(limit=int(limit))

    def get_candidates_queue() -> list[dict]:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, ts_utc, symbol, strategy_family, status, decision, "
                "rationale FROM strategy_candidates WHERE status = 'candidate' "
                "ORDER BY ts_utc DESC LIMIT ?",
                (_OUTPUT_ROW_CAP,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_pnl(*, days: int = 7) -> dict:
        days = int(days)
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, symbol, pnl_dollars, opened_at, closed_at, status
                FROM trades
                WHERE datetime(COALESCE(closed_at, opened_at)) >=
                      datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchall()
        per_sid: dict[int, float] = {}
        per_day: dict[str, float] = {}
        total = 0.0
        for r in rows:
            pnl = float(r["pnl_dollars"] or 0.0)
            total += pnl
            sid = r["strategy_id"]
            if sid is not None:
                per_sid[int(sid)] = per_sid.get(int(sid), 0.0) + pnl
            day_key = str(r["closed_at"] or r["opened_at"] or "")[:10]
            if day_key:
                per_day[day_key] = per_day.get(day_key, 0.0) + pnl
        return {
            "total_pnl": total,
            "n_trades": len(rows),
            "per_strategy": [
                {"strategy_id": sid, "pnl": v} for sid, v in per_sid.items()
            ],
            "per_day": [{"date": d, "pnl": v} for d, v in sorted(per_day.items())],
        }

    def get_regime_tag() -> dict:
        from src.research.regime import tag_regime

        return tag_regime(db_path=db_path)

    def get_recent_briefs(*, limit: int = 3) -> list[dict]:
        limit = max(1, min(int(limit), 10))
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT brief_date, summary_json FROM daily_briefs "
                "ORDER BY brief_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                summary = json.loads(r["summary_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                summary = {}
            out.append({"brief_date": r["brief_date"], "summary": summary})
        return out

    def get_recent_experiments(*, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, ts_utc, symbol, experiment_type, name, status, summary "
                "FROM research_experiments ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_open_positions() -> list[dict]:
        # Surface DB-recorded open trades. The caller (PM) can cross-reference
        # against IBKR via the deterministic reconcile job — we don't open new
        # IBKR sessions just to enumerate.
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, strategy_id, symbol, direction, qty, entry_fill, "
                "stop_price, target_price, status, opened_at "
                "FROM trades WHERE status = 'open' ORDER BY opened_at DESC "
                "LIMIT ?",
                (_OUTPUT_ROW_CAP,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def query_db(*, sql: str) -> dict:
        ok, err = _is_safe_select(sql)
        if not ok:
            return {"ok": False, "error": err, "rows": []}
        try:
            with _connect(db_path) as conn:
                cur = conn.execute(sql)
                rows = cur.fetchmany(_OUTPUT_ROW_CAP)
                cols = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"sqlite: {exc}", "rows": []}
        out_rows = [dict(zip(cols, r)) for r in rows]
        # Cap by serialized size.
        try:
            blob = json.dumps(out_rows, default=str)
        except (TypeError, ValueError):
            return {"ok": False, "error": "rows not json-serializable", "rows": []}
        if len(blob) > _OUTPUT_BYTES_CAP:
            # Trim until under cap.
            while out_rows and len(json.dumps(out_rows, default=str)) > _OUTPUT_BYTES_CAP:
                out_rows.pop()
        return {"ok": True, "rows": out_rows, "n_rows": len(out_rows)}

    # ---- backtesting tools ----------------------------------------------

    def run_judas_threshold_sweep(*, symbol: str, **kwargs) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        from src.tools import research_tools as rt

        payload = {"symbol": sym, **kwargs}
        try:
            raw = rt.judas_parameter_sweep_tool.run(input_json=json.dumps(payload, default=str))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"sweep failed: {exc}"}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"ok": False, "error": "sweep returned non-JSON", "raw": str(raw)[:400]}
        # Cap top results.
        if isinstance(data, dict):
            for k in ("top", "results", "top_results"):
                v = data.get(k)
                if isinstance(v, list) and len(v) > 10:
                    data[k] = v[:10]
        return {"ok": True, "result": data}

    def run_walk_forward(*, symbol: str, params: dict | None = None) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        from src.tools import research_tools as rt

        payload: dict[str, Any] = {"symbol": sym}
        if params:
            payload["parameters"] = params
        try:
            raw = rt.walk_forward_tool.run(input_json=json.dumps(payload, default=str))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"walk_forward failed: {exc}"}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"ok": False, "error": "walk_forward returned non-JSON"}
        return {"ok": True, "result": data}

    # ---- action tools (atomic, code-enforced) ----------------------------

    def retire_strategy_tool(*, id: int, reason: str) -> dict:
        from src import strategy_registry

        sid = int(id)
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason required"}
        # Snapshot metrics before retire.
        snap: dict = {}
        try:
            from src.research.live_review import compute_live_metrics

            m = compute_live_metrics(db_path=db_path, strategy_id=sid)
            snap = {
                "pf_20": m.pf_20,
                "expectancy_20": m.expectancy_20,
                "n_closed_trades": m.n_closed_trades,
                "total_realized_pnl": m.total_realized_pnl,
            }
        except Exception:  # noqa: BLE001
            snap = {}
        try:
            demotion_id = strategy_registry.retire_strategy(
                strategy_id=sid, reason=reason, metrics_snapshot=snap,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "demotion_id": int(demotion_id)}

    def propose_candidate_tool(
        *,
        symbol: str,
        strategy_family: str,
        params: dict,
        metrics: dict | None = None,
        rationale: str = "",
    ) -> dict:
        from src import strategy_registry

        sym = str(symbol).upper()
        if not isinstance(params, dict) or "symbol" not in params:
            params = dict(params or {})
            params["symbol"] = sym
        try:
            cid = strategy_registry.create_candidate(
                symbol=sym,
                strategy_family=str(strategy_family),
                params=dict(params),
                metrics=dict(metrics or {}),
                decision="pm_agent_propose",
                rationale=str(rationale or ""),
                status="candidate",
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "candidate_id": int(cid)}

    def promote_candidate_tool(*, id: int, notes: str = "") -> dict:
        from src import strategy_registry

        cid = int(id)
        try:
            new = strategy_registry.promote_candidate(cid, notes=notes or None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "new_strategy_id": int(new.id), "version": int(new.version)}

    def modify_strategy_params_tool(
        *, id: int, new_params: dict, rationale: str
    ) -> dict:
        """Atomic retire+promote in a single transaction.

        Per the brief and advisor review: ``promote_candidate`` computes
        ``next_version`` from the currently-active row only, so the naïve
        retire-then-promote sequence yields ``version=1``. We do a single
        explicit transaction here that retires the old row, writes an
        ``auto_demotions`` audit row, and inserts a new ``active_strategies``
        row at ``old.version + 1`` with the new params. A
        ``strategy_candidates`` row is written first with status='promoted'
        so the audit trail also shows the modify.
        """
        sid = int(id)
        if not isinstance(new_params, dict):
            return {"ok": False, "error": "new_params must be dict"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}

        from src.db.models import init_db

        init_db(db_path)

        # Snapshot live metrics outside the transaction (read-only).
        snap: dict = {}
        try:
            from src.research.live_review import compute_live_metrics

            m = compute_live_metrics(db_path=db_path, strategy_id=sid)
            snap = {
                "pf_20": m.pf_20,
                "expectancy_20": m.expectancy_20,
                "n_closed_trades": m.n_closed_trades,
                "total_realized_pnl": m.total_realized_pnl,
            }
        except Exception:  # noqa: BLE001
            snap = {}

        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ? AND state = 'active'",
                (sid,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {
                    "ok": False,
                    "error": f"active strategy {sid} not found or not active",
                }
            symbol = str(row["symbol"])
            family = str(row["strategy_family"])
            old_version = int(row["version"])
            old_params_json = str(row["params_json"] or "{}")
            now = _utc_now()

            # Ensure new_params has identifying keys for validation parity.
            merged = dict(new_params)
            merged.setdefault("symbol", symbol)
            try:
                old_params = json.loads(old_params_json)
            except (TypeError, json.JSONDecodeError):
                old_params = {}
            for alt in ("strategy_name", "strategy_family"):
                if alt not in merged and alt in old_params:
                    merged[alt] = old_params[alt]

            # 1) Audit: write candidate row showing the modify intent.
            cur = conn.execute(
                """
                INSERT INTO strategy_candidates
                    (ts_utc, symbol, strategy_family, source_experiment_id,
                     params_json, metrics_json, decision, rationale, status)
                VALUES (?, ?, ?, NULL, ?, ?, 'pm_agent_modify', ?, 'promoted-from-modify')
                """,
                (
                    now,
                    symbol,
                    family,
                    json.dumps(merged, default=str),
                    json.dumps(snap, default=str),
                    rationale,
                ),
            )
            candidate_id = int(cur.lastrowid)

            # 2) Insert new active row at old_version + 1 BEFORE retiring old,
            #    so external readers never see a zero-active window.
            cur = conn.execute(
                """
                INSERT INTO active_strategies
                    (symbol, strategy_family, version, params_json, metrics_json,
                     source_candidate_id, state, activated_at_utc, notes)
                VALUES (?, ?, ?, ?, '{}', ?, 'active', ?, ?)
                """,
                (
                    symbol,
                    family,
                    old_version + 1,
                    json.dumps(merged, default=str),
                    candidate_id,
                    now,
                    f"PM agent modify: {rationale}",
                ),
            )
            new_strategy_id = int(cur.lastrowid)

            # 3) Retire old active row.
            conn.execute(
                """
                UPDATE active_strategies
                SET state = 'retired',
                    deactivated_at_utc = ?,
                    notes = COALESCE(notes, '') || ?
                WHERE id = ?
                """,
                (now, f"\nAuto-modified: {rationale}", sid),
            )

            # 4) Auto_demotions audit row preserves the snapshot.
            cur = conn.execute(
                """
                INSERT INTO auto_demotions
                    (ts_utc, strategy_id, symbol, strategy_family, version,
                     params_json, metrics_snapshot_json, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    sid,
                    symbol,
                    family,
                    old_version,
                    old_params_json,
                    json.dumps(snap, default=str),
                    f"pm_agent_modify: {rationale}",
                ),
            )
            demotion_id = int(cur.lastrowid)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            conn.close()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": True,
            "demotion_id": demotion_id,
            "new_strategy_id": new_strategy_id,
            "version": old_version + 1,
        }

    def place_paper_order(
        *,
        symbol: str,
        side: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        rationale: str,
    ) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        side_u = str(side).upper()
        if side_u not in ("BUY", "SELL"):
            return {"ok": False, "error": "side must be BUY or SELL"}
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            return {"ok": False, "error": "quantity must be int"}
        if qty <= 0:
            return {"ok": False, "error": "quantity must be > 0"}
        try:
            stop = float(stop_price)
            tgt = float(target_price)
        except (TypeError, ValueError):
            return {"ok": False, "error": "stop_price/target_price must be numeric"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}

        if _BROKER_DRY_RUN:
            log.info("pm_agent.place_paper_order.dry_run", extra={"symbol": sym})
            return {"ok": False, "reason": "dry_run"}

        # Insert signals row first so the trade is attributable even on broker
        # failure.
        direction = "long" if side_u == "BUY" else "short"
        from src.db.models import init_db

        init_db(db_path)
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO signals
                    (ts_utc, symbol, strategy_id, strategy_family, strategy_version,
                     direction, quality_score, risk_decision, entry, stop, target,
                     rationale, agent_notes, raw_llm_output, created_at)
                VALUES (?, ?, NULL, 'pm_agent', NULL, ?, NULL, 'PM_AGENT', NULL, ?, ?,
                        ?, ?, NULL, ?)
                """,
                (
                    _utc_now(),
                    sym,
                    direction,
                    stop,
                    tgt,
                    rationale,
                    json.dumps({"source": "pm_agent"}),
                    _utc_now(),
                ),
            )
            signal_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        # Resolve broker config and place via the deterministic seam.
        try:
            from src.config import load_config

            cfg = load_config()
            ibkr = cfg.ibkr
            order = _place_bracket(
                symbol=sym,
                side=side_u,
                quantity=qty,
                stop_price=stop,
                target_price=tgt,
                host=ibkr.host,
                port=ibkr.port,
                client_id=ibkr.exec_client_id,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"broker failed: {type(exc).__name__}: {exc}",
                "signal_id": signal_id,
            }

        return {
            "ok": True,
            "signal_id": signal_id,
            "ibkr_order_ids": [
                order.get("parent_order_id"),
                order.get("tp_order_id"),
                order.get("sl_order_id"),
            ],
            "order": order,
        }

    return {
        "get_active_strategies": _safe_tool(get_active_strategies),
        "get_strategy_detail": _safe_tool(get_strategy_detail),
        "get_workshop_leaderboard": _safe_tool(get_workshop_leaderboard),
        "get_candidates_queue": _safe_tool(get_candidates_queue),
        "get_recent_pnl": _safe_tool(get_recent_pnl),
        "get_regime_tag": _safe_tool(get_regime_tag),
        "get_recent_briefs": _safe_tool(get_recent_briefs),
        "get_recent_experiments": _safe_tool(get_recent_experiments),
        "get_open_positions": _safe_tool(get_open_positions),
        "query_db": _safe_tool(query_db),
        "run_judas_threshold_sweep": _safe_tool(run_judas_threshold_sweep),
        "run_walk_forward": _safe_tool(run_walk_forward),
        "retire_strategy": _safe_tool(retire_strategy_tool),
        "propose_candidate": _safe_tool(propose_candidate_tool),
        "promote_candidate": _safe_tool(promote_candidate_tool),
        "modify_strategy_params": _safe_tool(modify_strategy_params_tool),
        "place_paper_order": _safe_tool(place_paper_order),
    }


# Action tools whose invocations populate PMDecisionResult.actions_taken.
_ACTION_TOOL_NAMES = {
    "retire_strategy",
    "propose_candidate",
    "promote_candidate",
    "modify_strategy_params",
    "place_paper_order",
    "run_judas_threshold_sweep",
    "run_walk_forward",
}


def _tool_schemas() -> list[dict]:
    sym_enum = sorted(_VALID_SYMBOLS)
    return [
        {
            "type": "function",
            "function": {
                "name": "get_active_strategies",
                "description": "List active strategies with rolling metrics.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_strategy_detail",
                "description": "Full params + metrics for one active strategy id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_workshop_leaderboard",
                "description": "Top-N strategies from knowledge_base/buffet.yaml.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_candidates_queue",
                "description": "Pending strategy_candidates rows.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_pnl",
                "description": "Realized P&L total + per-strategy + per-day.",
                "parameters": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "default": 7}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_regime_tag",
                "description": "Coarse market regime tag (vol_regime/trend/leaders).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_briefs",
                "description": "Last N daily briefs.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 3}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_experiments",
                "description": "Recent research_experiments rows.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 10}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_open_positions",
                "description": "DB-recorded open trades.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_db",
                "description": (
                    "Run a single SELECT statement (read-only). "
                    "Multi-statement, PRAGMA, ATTACH, and writes are rejected."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_judas_threshold_sweep",
                "description": "Backtest Judas thresholds for a symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "max_combinations": {"type": "integer"},
                        "min_trades": {"type": "integer"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_walk_forward",
                "description": "Walk-forward validation for a symbol+params.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "params": {"type": "object"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retire_strategy",
                "description": (
                    "Atomically retire an active strategy. Writes auto_demotions audit row."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_candidate",
                "description": "Insert a strategy_candidates row with status=candidate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "strategy_family": {"type": "string"},
                        "params": {"type": "object"},
                        "metrics": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["symbol", "strategy_family", "params", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "promote_candidate",
                "description": "Atomically promote a candidate to active (v+1).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "modify_strategy_params",
                "description": (
                    "Atomic retire-old + promote-new with new params. "
                    "Writes auto_demotions audit and a v+1 active row."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "new_params": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "new_params", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "place_paper_order",
                "description": (
                    "Place a paper bracket via the deterministic broker path. "
                    "Inserts a signals row first; returns ibkr_order_ids on success."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "side": {"type": "string", "enum": ["BUY", "SELL"]},
                        "quantity": {"type": "integer"},
                        "stop_price": {"type": "number"},
                        "target_price": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "symbol", "side", "quantity",
                        "stop_price", "target_price", "rationale",
                    ],
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# litellm seam
# ---------------------------------------------------------------------------


def _call_llm(
    *, messages: list[dict], tools: list[dict], model: str, timeout_s: int
) -> Any:
    """Wrap litellm so tests can monkeypatch this single seam."""
    import litellm  # type: ignore[import-not-found]

    return litellm.completion(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        timeout=timeout_s,
        temperature=0.2,
    )


def _extract_message(response: Any) -> dict:
    try:
        choice = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        choice = getattr(response, "choices", [None])[0]
        choice = getattr(choice, "message", None) or {}
    if hasattr(choice, "model_dump"):
        return choice.model_dump()
    if hasattr(choice, "to_dict"):
        return choice.to_dict()
    if isinstance(choice, dict):
        return dict(choice)
    return {"role": "assistant", "content": str(choice)}


def _tool_calls_from(message: dict) -> list[dict]:
    raw = message.get("tool_calls") or []
    out: list[dict] = []
    for tc in raw:
        if isinstance(tc, dict):
            out.append(tc)
        elif hasattr(tc, "model_dump"):
            out.append(tc.model_dump())
        elif hasattr(tc, "to_dict"):
            out.append(tc.to_dict())
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_pm_decision(
    *,
    db_path: str,
    turn_budget: int = 30,
    time_budget_s: int = 1800,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> PMDecisionResult:
    """Run one PM cycle. Pure function over ``db_path`` plus the LLM seam.

    No-key fallback: when ``MINIMAX_API_KEY`` is missing this returns
    immediately with ``fallback_used=True`` and zero actions — by design.
    The operator's daily check picks up the no-op narrative.
    """
    started = time.time()

    if os.environ.get("JUDAS_PM_AGENT_INHIBIT") == "1":
        log.info("pm_agent.inhibited_by_env")
        return PMDecisionResult(
            success=True,
            actions_taken=[],
            narrative="PM agent inhibited via JUDAS_PM_AGENT_INHIBIT (test/safety mode); no actions taken.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    if not os.environ.get("MINIMAX_API_KEY"):
        log.warning("pm_agent.no_api_key.fallback_noop")
        return PMDecisionResult(
            success=True,
            actions_taken=[],
            narrative="M2.7 unreachable (MINIMAX_API_KEY missing); no actions taken today.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    tools = _make_tools(db_path=db_path)
    schemas = _tool_schemas()

    system_prompt = PM_SYSTEM_PROMPT.format(
        turn_budget=turn_budget, time_budget_s=time_budget_s
    )
    try:
        date_et = datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        date_et = datetime.now(timezone.utc).isoformat()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"It's {date_et}. Trade."},
    ]

    actions: list[PMAction] = []
    turn = 0
    error: str | None = None
    final_text = ""

    while turn < turn_budget:
        elapsed = time.time() - started
        if elapsed >= time_budget_s:
            error = f"time budget exhausted after {elapsed:.1f}s"
            break
        remaining = max(1, int(time_budget_s - elapsed))
        try:
            response = _call_llm(
                messages=messages,
                tools=schemas,
                model=minimax_model,
                timeout_s=min(remaining, 300),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            error = f"llm call failed: {exc}"
            break

        msg = _extract_message(response)
        messages.append(msg)
        turn += 1

        tool_calls = _tool_calls_from(msg)
        if not tool_calls:
            final_text = str(msg.get("content") or "").strip()
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            arg_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(arg_str) if isinstance(arg_str, str) else dict(arg_str)
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_fn = tools.get(name)
            if tool_fn is None:
                result: Any = {"ok": False, "error": f"unknown tool {name!r}"}
            else:
                try:
                    result = tool_fn(**args)
                except TypeError as exc:
                    result = {"ok": False, "error": f"bad args: {exc}"}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"tool failed: {exc}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name,
                    "content": json.dumps(result, default=str),
                }
            )
            if name in _ACTION_TOOL_NAMES:
                target_id = None
                if isinstance(args.get("id"), int):
                    target_id = args["id"]
                actions.append(
                    PMAction(
                        action=name,
                        target_id=target_id,
                        payload=dict(args),
                        rationale=str(args.get("rationale") or args.get("reason") or args.get("notes") or ""),
                        tool_result=result if isinstance(result, dict) else {"value": result},
                    )
                )

    elapsed_s = time.time() - started
    if not final_text:
        # Best-effort: stitch a narrative from the last assistant message + actions.
        last_assistant = next(
            (m.get("content") for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        if last_assistant:
            final_text = str(last_assistant).strip()
        else:
            final_text = (
                f"PM cycle ended with {len(actions)} action(s); turns={turn}, "
                f"elapsed={elapsed_s:.1f}s."
            )

    return PMDecisionResult(
        success=error is None,
        actions_taken=actions,
        narrative=final_text,
        turns_used=turn,
        elapsed_s=elapsed_s,
        fallback_used=False,
        raw_messages=messages,
        error=error,
    )
