"""Rich strategy stats — shared by the agent's get_leaderboard tool,
the kickoff briefing block, and the dashboard API.

Single source of truth. Pure SQL + Python math. No LLM, no side effects.

For each active strategy we compute:
  Live (from trades table):
    - live_n_total: closed-trade count
    - live_n_real: closed trades with real IBKR fills (exit_reason LIKE '%_real')
    - live_pnl_gross, live_pnl_net: total pnl gross / cost-adjusted
    - live_pf, live_pf_net: profit factor gross / net (None when n<3)
    - live_winrate
    - avg_win, avg_loss, avg_trade: dollar averages
    - expectancy_dollars
    - max_consec_losers
    - best_trade, worst_trade
    - longs_n / shorts_n + per-side pnl
    - days_since_last_fire
    - first_fire_utc
    - last_real_fill_utc
  Backtest (from metrics_json on active_strategies):
    - backtest_pf, backtest_n, backtest_winrate
    - backtest_expectancy_r, backtest_max_dd
    - backtest_robustness, backtest_walk_forward_id (when linkable)
    - cost_model (param-stamped version, or None)
  Flags (derived):
    - unproven_live: live_n_real < 3
    - gap_flag: backtest_pf >= 2 AND live_n_real >= 3 AND live_pf_net <= 0
    - stale: days_since_last_fire >= 7 (or never fired with age > 7d)
    - displacement_r_bug: params contain 'displacement_r' but no 'displacement'
      (per reviewer finding 2026-05-26: judas_native engine reads 'displacement'
       not 'displacement_r'; rows with only displacement_r fire on defaults)
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Cost-model integration. Imported lazily — keeps this module importable
# even if cost_model has issues at import time.
# ---------------------------------------------------------------------------


def _cost_per_round_trip(symbol: str, exit_reason: str | None) -> float:
    try:
        from src.tools.cost_model import round_trip_cost
        return float(round_trip_cost(symbol, exit_reason or "stop"))
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _clean_number(x: Any) -> Any:
    """Replace NaN / Infinity with None so the result is JSON-safe.
    json.dumps emits 'NaN' / 'Infinity' tokens for these, which are
    invalid JSON and silently break the frontend's JSON.parse."""
    if isinstance(x, float):
        if math.isnan(x) or math.isinf(x):
            return None
    return x


def _pf(gross_win: float, gross_loss: float) -> float | None:
    if gross_loss > 0:
        return round(gross_win / gross_loss, 3)
    # All wins or no trades: return None so the JSON response stays valid.
    # The "PF=∞ when all wins" semantics can be inferred by the caller from
    # live_n + win count if needed; never emit float('inf') here.
    return None


def _max_consec_losers(pnls_recent_first: list[float]) -> int:
    best = 0
    cur = 0
    for p in pnls_recent_first:
        if p < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


# ---------------------------------------------------------------------------
# Per-strategy stats
# ---------------------------------------------------------------------------


def strategy_stats(conn: sqlite3.Connection, strategy_row: sqlite3.Row | dict) -> dict:
    """Compute the rich stats dict for one active_strategies row.

    Caller passes an open SQLite connection (row_factory=sqlite3.Row) and the
    strategy row itself. We pull live trades + backtest metrics + flags.
    """
    sid = int(strategy_row["id"])
    symbol = str(strategy_row["symbol"])
    family = str(strategy_row["strategy_family"])
    try:
        params = json.loads(strategy_row["params_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    try:
        metrics = json.loads(strategy_row["metrics_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        metrics = {}

    # --- Live trades for this strategy_id ----------------------------------
    rows = conn.execute(
        """
        SELECT id, direction, pnl_dollars, exit_reason, opened_at, closed_at,
               qty, entry_fill, exit_fill, status
        FROM trades
        WHERE strategy_id = ? AND status='closed'
        ORDER BY datetime(COALESCE(closed_at, opened_at)) DESC
        """,
        (sid,),
    ).fetchall()

    n_total = len(rows)
    pnls_gross = [_safe_float(r["pnl_dollars"]) for r in rows]
    real_idx = [i for i, r in enumerate(rows)
                if r["exit_reason"] and str(r["exit_reason"]).endswith("_real")]

    n_real = len(real_idx)
    pnl_gross_total = round(sum(pnls_gross), 2)
    cost_total = sum(
        _cost_per_round_trip(symbol, r["exit_reason"]) * _safe_int(r["qty"] or 1)
        for r in rows
    )
    pnl_net_total = round(pnl_gross_total - cost_total, 2)

    # Win/loss + PF (use real fills when available, else total)
    if n_real >= 3:
        sample_rows = [rows[i] for i in real_idx]
    else:
        sample_rows = rows
    sample_pnls_gross = [_safe_float(r["pnl_dollars"]) for r in sample_rows]
    sample_costs = [
        _cost_per_round_trip(symbol, r["exit_reason"]) * _safe_int(r["qty"] or 1)
        for r in sample_rows
    ]
    sample_pnls_net = [g - c for g, c in zip(sample_pnls_gross, sample_costs)]

    wins_gross = [p for p in sample_pnls_gross if p > 0]
    losses_gross = [p for p in sample_pnls_gross if p < 0]
    wins_net = [p for p in sample_pnls_net if p > 0]
    losses_net = [p for p in sample_pnls_net if p < 0]

    live_pf = _pf(sum(wins_gross), -sum(losses_gross)) if len(sample_pnls_gross) >= 3 else None
    live_pf_net = _pf(sum(wins_net), -sum(losses_net)) if len(sample_pnls_net) >= 3 else None
    live_winrate = (len(wins_net) / len(sample_pnls_net)) if sample_pnls_net else None
    # JSON-safety pass on every numeric metric. The metrics_json blob can
    # contain NaN entries from old experiments — those would re-emit as the
    # bare token "NaN" and break the dashboard's JSON.parse.
    live_pf = _clean_number(live_pf)
    live_pf_net = _clean_number(live_pf_net)
    live_winrate = _clean_number(live_winrate)

    avg_win = round(sum(wins_net) / len(wins_net), 2) if wins_net else 0.0
    avg_loss = round(sum(losses_net) / len(losses_net), 2) if losses_net else 0.0
    avg_trade = round(sum(sample_pnls_net) / len(sample_pnls_net), 2) if sample_pnls_net else 0.0
    expectancy_dollars = avg_trade  # alias — clearer name for agents
    best_trade = round(max(sample_pnls_net), 2) if sample_pnls_net else None
    worst_trade = round(min(sample_pnls_net), 2) if sample_pnls_net else None
    max_consec_loss = _max_consec_losers(sample_pnls_net)

    # By direction
    longs = [r for r in rows if r["direction"] == "long"]
    shorts = [r for r in rows if r["direction"] == "short"]
    longs_pnl = round(sum(_safe_float(r["pnl_dollars"]) for r in longs), 2)
    shorts_pnl = round(sum(_safe_float(r["pnl_dollars"]) for r in shorts), 2)

    # Timing
    first_fire = None
    last_fire = None
    last_real_fill = None
    if n_total:
        first_fire_dt = min(filter(None, (_parse_iso(r["opened_at"]) for r in rows)),
                            default=None)
        last_fire_dt = max(filter(None, (_parse_iso(r["opened_at"]) for r in rows)),
                           default=None)
        if first_fire_dt:
            first_fire = first_fire_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if last_fire_dt:
            last_fire = last_fire_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if real_idx:
        last_real_dt = max(
            filter(None, (_parse_iso(rows[i]["closed_at"]) for i in real_idx)),
            default=None,
        )
        if last_real_dt:
            last_real_fill = last_real_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    activated_dt = _parse_iso(strategy_row["activated_at_utc"])
    last_fire_dt2 = _parse_iso(last_fire)
    now = datetime.now(timezone.utc)
    age_days = (now - activated_dt).days if activated_dt else None
    if last_fire_dt2:
        days_since_last_fire = max(0, (now - last_fire_dt2).days)
    elif activated_dt:
        days_since_last_fire = max(0, (now - activated_dt).days)
    else:
        days_since_last_fire = None

    # --- Backtest metrics --------------------------------------------------
    # For direct zoo insertions (metrics_json = {"inserted_directly": true}),
    # the metrics dict is a placeholder sentinel — read params.backtest_override
    # FIRST so correct workshop backtest values are preserved and the Judas
    # auto-link fallback is not triggered.
    # See finding 4e8e4695: {"inserted_directly": true} is truthy, which was
    # short-circuiting the backtest_override check and causing PF corruption.
    bt_pf = None
    bt_trades = _safe_int(metrics.get("trades") or metrics.get("total_test_trades") or 0)
    bt_inferred_from_exp_id = None
    if params.get("backtest_override", {}).get("backtest_pf") is not None:
        ov = params["backtest_override"]
        candidate = _safe_float(ov.get("backtest_pf"), None)
        if candidate is not None and not (math.isnan(candidate) or math.isinf(candidate)):
            bt_pf = candidate
            bt_trades = _safe_int(ov.get("backtest_n"), 0) or bt_trades
            bt_inferred_from_exp_id = None
    if bt_pf is None:
        bt_pf_raw = (metrics.get("profit_factor")
                     or metrics.get("avg_test_profit_factor")
                     or metrics.get("pf_20"))
        if bt_pf_raw is not None:
            try:
                v = float(bt_pf_raw)
                if not (math.isnan(v) or math.isinf(v)):
                    bt_pf = v
            except (TypeError, ValueError):
                pass
    # Fallback: this row may have been created by modify_strategy_params with
    # no backtest attached. Look for the most recent walk_forward experiment
    # for this (symbol, family/engine) and surface it as a reference value.
    if bt_pf is None:
        try:
            ref = conn.execute(
                """
                SELECT id, metrics_json
                FROM research_experiments
                WHERE experiment_type='walk_forward'
                  AND symbol = ?
                ORDER BY id DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if ref:
                try:
                    rm = json.loads(ref["metrics_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    rm = {}
                p = rm.get("avg_test_profit_factor")
                t = rm.get("total_test_trades")
                if p is not None:
                    pv = _safe_float(p, None)
                    if pv is not None and not math.isnan(pv) and not math.isinf(pv):
                        bt_pf = pv
                        bt_trades = _safe_int(t, 0) or bt_trades
                        bt_inferred_from_exp_id = int(ref["id"])
        except Exception:  # noqa: BLE001
            pass
    bt_winrate = _safe_float(metrics.get("winrate"), None) if metrics.get("winrate") is not None else None
    bt_expectancy_r = _safe_float(metrics.get("expectancy_r")
                                  or metrics.get("avg_test_expectancy_r"), None) \
        if (metrics.get("expectancy_r") is not None
            or metrics.get("avg_test_expectancy_r") is not None) else None
    bt_max_dd = _safe_float(metrics.get("max_drawdown_dollars")
                            or metrics.get("avg_test_max_drawdown_dollars"), None) \
        if (metrics.get("max_drawdown_dollars") is not None
            or metrics.get("avg_test_max_drawdown_dollars") is not None) else None
    bt_robustness = _safe_float(metrics.get("robustness_score"), None) \
        if metrics.get("robustness_score") is not None else None
    bt_walk_forward_id = metrics.get("source_walk_forward_id") \
        or metrics.get("walk_forward_id")
    cost_model = params.get("cost_model") or metrics.get("cost_model")

    # --- Flags -------------------------------------------------------------
    unproven_live = (n_real < 3)
    gap_flag = (
        (bt_pf is not None and bt_pf >= 2.0)
        and n_real >= 3
        and (live_pf_net is None or live_pf_net <= 0)
    )
    stale = (days_since_last_fire is not None and days_since_last_fire >= 7)
    # judas_native engine reads `displacement` not `displacement_r` —
    # rows with only the _r variant fire defaults (per 2026-05-26 finding).
    displacement_r_bug = bool(
        params.get("execution_engine") == "judas_native"
        and "displacement_r" in params
        and "displacement" not in params
    )
    # The active row has no real backtest metrics attached. Either it was
    # created via modify_strategy_params without re-backtesting, or the
    # metrics_json blob is a placeholder ({pf_20: NaN, ...}). The leaderboard
    # may have backfilled from a sibling walk_forward; flag either way.
    no_backtest_metrics = (
        bt_pf is None
        or not metrics
        or (isinstance(metrics.get("pf_20"), float) and math.isnan(metrics.get("pf_20")))
    )

    result = {
        "id": sid,
        "symbol": symbol,
        "strategy_family": family,
        "version": _safe_int(strategy_row["version"]),
        "strategy_name": params.get("strategy_name") or "?",
        "execution_engine": params.get("execution_engine"),
        "activated_at_utc": str(strategy_row["activated_at_utc"] or ""),
        "age_days": age_days,
        # Live — gross
        "live_n_total": n_total,
        "live_n_real": n_real,
        "live_pnl_gross": pnl_gross_total,
        "live_pnl_net": pnl_net_total,
        "live_pf": live_pf,
        "live_pf_net": live_pf_net,
        "live_winrate": round(live_winrate, 3) if live_winrate is not None else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "avg_trade": avg_trade,
        "expectancy_dollars": expectancy_dollars,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "max_consec_loss": max_consec_loss,
        "longs_n": len(longs),
        "longs_pnl": longs_pnl,
        "shorts_n": len(shorts),
        "shorts_pnl": shorts_pnl,
        "first_fire_utc": first_fire,
        "last_fire_utc": last_fire,
        "last_real_fill_utc": last_real_fill,
        "days_since_last_fire": days_since_last_fire,
        # Backtest
        "backtest_pf": round(bt_pf, 3) if bt_pf is not None else None,
        "backtest_n": bt_trades,
        "backtest_winrate": round(bt_winrate, 3) if bt_winrate is not None else None,
        "backtest_expectancy_r": round(bt_expectancy_r, 3) if bt_expectancy_r is not None else None,
        "backtest_max_dd": round(bt_max_dd, 2) if bt_max_dd is not None else None,
        "backtest_robustness": round(bt_robustness, 3) if bt_robustness is not None else None,
        "backtest_walk_forward_id": bt_walk_forward_id,
        "cost_model": cost_model,
        "backtest_inferred_from_experiment_id": bt_inferred_from_exp_id,
        # Flags
        "unproven_live": unproven_live,
        "gap_flag": gap_flag,
        "stale": stale,
        "displacement_r_bug": displacement_r_bug,
        "no_backtest_metrics": no_backtest_metrics,
    }
    # Final JSON-safety pass: any field that ended up as NaN / Infinity
    # gets replaced with None. This catches values that leaked through
    # from metrics_json blobs containing legacy NaN tokens.
    return {k: _clean_number(v) for k, v in result.items()}


def all_active_stats(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    """Rich stats for every active_strategies row, sorted cost-aware-first,
    then by live_pf_net desc, then backtest_pf desc.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, symbol, strategy_family, version, params_json, metrics_json,
               activated_at_utc
        FROM active_strategies
        WHERE state='active'
        """
    ).fetchall()
    out: list[dict] = [strategy_stats(conn, r) for r in rows]

    def _sort_key(r: dict) -> tuple:
        # cost_model present first; then live_pf_net desc (None last);
        # then backtest_pf desc (None last).
        return (
            0 if r.get("cost_model") else 1,
            -(r["live_pf_net"] if r["live_pf_net"] is not None else -1e9),
            -(r["backtest_pf"] if r["backtest_pf"] is not None else -1e9),
        )

    out.sort(key=_sort_key)
    return out[: max(1, int(limit))]


def coverage_summary(conn: sqlite3.Connection) -> dict:
    """Symbols covered vs uncovered."""
    all_syms = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}
    covered_rows = conn.execute(
        "SELECT DISTINCT symbol FROM active_strategies WHERE state='active'"
    ).fetchall()
    covered = {str(r["symbol"]) for r in covered_rows}
    return {
        "covered": sorted(covered),
        "uncovered": sorted(all_syms - covered),
        "total_active_symbols": len(covered),
        "total_target_symbols": len(all_syms),
    }


def sleeve_summary(conn: sqlite3.Connection, *, starting: float = 5000.0) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS n_total,
               COALESCE(SUM(pnl_dollars), 0) AS pnl_total,
               SUM(CASE WHEN exit_reason LIKE '%_real' THEN 1 ELSE 0 END) AS n_real,
               COALESCE(SUM(CASE WHEN exit_reason LIKE '%_real' THEN pnl_dollars ELSE 0 END), 0) AS pnl_real
        FROM trades WHERE status='closed'
        """
    ).fetchone()
    n_total = _safe_int(row["n_total"])
    pnl_total = _safe_float(row["pnl_total"])
    n_real = _safe_int(row["n_real"])
    pnl_real = _safe_float(row["pnl_real"])

    # Archived (pre-reset) totals for context.
    arch_row = None
    try:
        arch_row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_dollars),0) AS pnl FROM trades_archive"
        ).fetchone()
    except sqlite3.OperationalError:
        pass
    arch_n = _safe_int(arch_row["n"]) if arch_row else 0
    arch_pnl = _safe_float(arch_row["pnl"]) if arch_row else 0.0

    current = starting + pnl_total
    growth_pct = ((current - starting) / starting * 100) if starting > 0 else 0.0
    return {
        "starting": starting,
        "current": round(current, 2),
        "growth_pct": round(growth_pct, 3),
        "trades_closed": n_total,
        "pnl_total": round(pnl_total, 2),
        "trades_real_fill": n_real,
        "pnl_real_fill": round(pnl_real, 2),
        "archived_trades": arch_n,
        "archived_pnl": round(arch_pnl, 2),
    }


def recent_walk_forward_candidates(conn: sqlite3.Connection, *, limit: int = 12) -> list[dict]:
    """Recent walk_forward experiments not yet active — potential promotions."""
    rows = conn.execute(
        """
        SELECT id, ts_utc, symbol, name, metrics_json
        FROM research_experiments
        WHERE experiment_type='walk_forward'
        ORDER BY id DESC LIMIT 80
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            m = json.loads(r["metrics_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        pf = m.get("avg_test_profit_factor")
        n_trades = m.get("total_test_trades")
        if pf is None or n_trades is None:
            continue
        try:
            pf_f = float(pf); n_f = int(n_trades)
        except (TypeError, ValueError):
            continue
        out.append({
            "experiment_id": int(r["id"]),
            "ts_utc": str(r["ts_utc"]),
            "symbol": str(r["symbol"]),
            "name": str(r["name"]),
            "backtest_pf": round(pf_f, 3),
            "backtest_n": n_f,
            "backtest_expectancy_r": _safe_float(m.get("avg_test_expectancy_r"), None)
                if m.get("avg_test_expectancy_r") is not None else None,
            "backtest_max_dd": _safe_float(m.get("avg_test_max_drawdown_dollars"), None)
                if m.get("avg_test_max_drawdown_dollars") is not None else None,
            "backtest_robustness": _safe_float(m.get("robustness_score"), None)
                if m.get("robustness_score") is not None else None,
            "cost_model": m.get("cost_model"),
        })
    out.sort(key=lambda r: (0 if r.get("cost_model") else 1, -r["backtest_pf"]))
    return out[: max(1, int(limit))]
