"""Phase 5 — exploratory research planner + executor.

When the OperatorFlow has nothing urgent to do (no retire-list, no recent
auto-demotions, no failure symptoms), it should *explore*: pick the most
promising research direction given current context, plan an experiment,
run it, and let any promote-able variant flow through the existing
``create_candidate`` path.

Surface:

    gather_explore_context(*, db_path)         -> dict
    plan_experiment(*, context)                -> ExplorePlan
    execute_plan(*, plan, db_path, timeout_s)  -> dict

The LLM path uses MiniMax M3 via ``litellm`` when ``MINIMAX_API_KEY`` is
set. On any failure (missing key, parse error, schema/allowlist violation,
network) the deterministic fallback runs. The fallback is the source of
truth.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.db.models import get_conn, init_db

log = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 30
_DEFAULT_EXEC_TIMEOUT_S = 600

ALLOWED_TOOLS = (
    "judas_threshold_sweep",
    "judas_parameter_sweep",
    "walk_forward",
    "buffet_zoo_sweep",
    "pair_sweep",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ExplorePlan:
    tool: str
    symbol: str
    params: dict = field(default_factory=dict)
    rationale: str = ""
    fallback_used: bool = False

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "symbol": self.symbol,
            "params": dict(self.params),
            "rationale": self.rationale,
            "fallback_used": self.fallback_used,
        }


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _strategy_label(row: Any) -> dict:
    """Compact label for an active_strategies row — no raw params blob."""
    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    return {
        "id": int(row["id"]),
        "symbol": str(row["symbol"]),
        "strategy_family": str(row["strategy_family"]),
        "strategy_name": str(params.get("strategy_name") or ""),
        "max_sweep_age_bars": params.get("max_sweep_age_bars"),
        "activated_at_utc": str(row["activated_at_utc"]),
    }


def _load_workshop_leaderboard_compact(limit: int = 5) -> list[dict]:
    """Top-N entries from knowledge_base/buffet.yaml, compact shape only."""
    path = _REPO_ROOT / "knowledge_base" / "buffet.yaml"
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    strategies = data.get("strategies") or []
    out: list[dict] = []
    for s in strategies:
        if not isinstance(s, dict):
            continue
        bt = s.get("backtest") or {}
        out.append(
            {
                "id": str(s.get("id", "")),
                "symbol": str(s.get("symbol", "")).upper(),
                "kind": str(s.get("kind", "")),
                "type": str(s.get("type", "")),
                "pf": float(bt.get("profit_factor") or 0.0),
                "trades": int(bt.get("trades") or 0),
                "params": s.get("params") or {},
            }
        )
    out.sort(key=lambda d: d["pf"], reverse=True)
    return out[:limit]


def gather_explore_context(*, db_path: str) -> dict:
    """Read DB + leaderboard, return a structured (≤4KB JSON) context dict."""
    init_db(db_path)
    now = datetime.now(timezone.utc)
    seven_d = now - timedelta(days=7)
    thirty_d = now - timedelta(days=30)

    with get_conn(db_path) as conn:
        active_rows = conn.execute(
            "SELECT id, symbol, strategy_family, params_json, activated_at_utc "
            "FROM active_strategies WHERE state = 'active' ORDER BY id"
        ).fetchall()
        active = [_strategy_label(r) for r in active_rows]

        # Most recent activation timestamp.
        last_activation_row = conn.execute(
            "SELECT MAX(activated_at_utc) AS m FROM active_strategies "
            "WHERE state = 'active'"
        ).fetchone()
        last_activation = (
            str(last_activation_row["m"]) if last_activation_row and last_activation_row["m"] else None
        )

        # Demotions in last 30 days (compact).
        demotion_rows = conn.execute(
            "SELECT id, ts_utc, strategy_id, symbol, strategy_family, reason "
            "FROM auto_demotions WHERE datetime(ts_utc) >= datetime(?) "
            "ORDER BY ts_utc DESC LIMIT 30",
            (thirty_d.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchall()
        demotions = [
            {
                "id": int(r["id"]),
                "ts_utc": str(r["ts_utc"]),
                "strategy_id": int(r["strategy_id"]),
                "symbol": str(r["symbol"]),
                "strategy_family": str(r["strategy_family"]),
                "reason": str(r["reason"] or "")[:120],
            }
            for r in demotion_rows
        ]
        last_demotion = demotions[0]["ts_utc"] if demotions else None

        # Last 7 daily briefs (most recent first).
        brief_rows = conn.execute(
            "SELECT brief_date, summary_json FROM daily_briefs "
            "ORDER BY brief_date DESC LIMIT 7"
        ).fetchall()
        briefs: list[dict] = []
        for r in brief_rows:
            try:
                summary = json.loads(r["summary_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                summary = {}
            regime = summary.get("regime") or {}
            surprises = summary.get("surprises") or []
            briefs.append(
                {
                    "brief_date": str(r["brief_date"]),
                    "regime": {
                        "vol_regime": regime.get("vol_regime", "mid"),
                        "trend": regime.get("trend", "mixed"),
                    },
                    "n_surprises": len(surprises),
                    "surprise_strategy_ids": [
                        int(s.get("strategy_id"))
                        for s in surprises[:3]
                        if isinstance(s, dict) and s.get("strategy_id") is not None
                    ],
                }
            )

    # Turnover signal: did anything change in active_strategies or did anything
    # auto-demote within the last 7 days?
    last_act_dt = _parse_iso_utc(last_activation)
    last_dem_dt = _parse_iso_utc(last_demotion)
    fresh_activation = last_act_dt is not None and last_act_dt >= seven_d
    fresh_demotion = last_dem_dt is not None and last_dem_dt >= seven_d
    no_turnover_7d = not (fresh_activation or fresh_demotion)

    leaderboard = _load_workshop_leaderboard_compact(limit=5)

    return {
        "now_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_strategies": active,
        "n_active": len(active),
        "last_activation_utc": last_activation,
        "last_demotion_utc": last_demotion,
        "no_turnover_7d": no_turnover_7d,
        "demotions_30d": demotions[:10],  # cap for size
        "briefs_7d": briefs,
        "leaderboard_top5": leaderboard,
    }


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


_SYMBOL_ROTATION = ("MGC", "MNQ", "MCL", "MET")


def _deterministic_plan(context: dict) -> ExplorePlan:
    """Source-of-truth planner. Always returns a valid ExplorePlan."""
    active = context.get("active_strategies") or []
    active_symbols = {str(a.get("symbol", "")).upper() for a in active}

    briefs = context.get("briefs_7d") or []
    most_recent = briefs[0] if briefs else None
    recent_regime = (most_recent or {}).get("regime") or {}
    vol_regime = str(recent_regime.get("vol_regime", "mid"))

    # Surprises in most-recent brief.
    if most_recent and (most_recent.get("n_surprises") or 0) > 0:
        sids = most_recent.get("surprise_strategy_ids") or []
        surprise_sid = sids[0] if sids else None
        # Find the surprising strategy in active set if present, else fall through.
        target = None
        for a in active:
            if surprise_sid is not None and a.get("id") == surprise_sid:
                target = a
                break
        if target is not None:
            return ExplorePlan(
                tool="walk_forward",
                symbol=str(target.get("symbol", "MGC")).upper(),
                params={
                    "parameters": {
                        "target_r": 2.0,
                        "stop_buffer_ticks": 2,
                        "min_sweep_ticks": 3,
                        "min_displacement_strength": 1.0,
                        "min_displacement_body_ratio": 0.5,
                        "max_sweep_age_bars": 2,
                        "require_fvg": False,
                        "session_filter": "ny_open",
                    },
                    "rolling_windows": 3,
                },
                rationale=(
                    f"surprise on strategy {surprise_sid} in last brief — "
                    "walk-forward to confirm robustness"
                ),
                fallback_used=True,
            )

    # No turnover in 7+ days → propose pair_sweep on top-PF leaderboard pair the
    # active set isn't using.
    if context.get("no_turnover_7d"):
        leaderboard = context.get("leaderboard_top5") or []
        unused = [
            entry
            for entry in leaderboard
            if str(entry.get("symbol", "")).upper() not in active_symbols
        ]
        target = unused[0] if unused else (leaderboard[0] if leaderboard else None)
        if target is not None:
            sym = str(target.get("symbol", "MGC")).upper()
            return ExplorePlan(
                tool="pair_sweep",
                symbol=sym,
                params={
                    "leaderboard_id": target.get("id"),
                    "type": target.get("type"),
                    "params": target.get("params") or {},
                },
                rationale=(
                    "no turnover in active set for 7+ days — probe leaderboard "
                    f"pair {target.get('id')} on {sym}"
                ),
                fallback_used=True,
            )
        # Fall through if no leaderboard.

    # High vol → judas_threshold_sweep on the active strategy with the loosest
    # max_sweep_age_bars (most permissive sweep age).
    if vol_regime == "high" and active:
        def _sweep_age(a: dict) -> int:
            v = a.get("max_sweep_age_bars")
            try:
                return int(v) if v is not None else -1
            except (TypeError, ValueError):
                return -1

        target = max(active, key=_sweep_age)
        sym = str(target.get("symbol", "MGC")).upper()
        return ExplorePlan(
            tool="judas_threshold_sweep",
            symbol=sym,
            params={"max_combinations": 32, "min_trades": 3},
            rationale=(
                f"recent regime is high-vol — re-sweep thresholds on "
                f"{sym} (loosest sweep_age)"
            ),
            fallback_used=True,
        )

    # Default: rotate parameter sweep across symbol cycle.
    n_briefs = len(briefs)
    sym = _SYMBOL_ROTATION[n_briefs % len(_SYMBOL_ROTATION)]
    return ExplorePlan(
        tool="judas_parameter_sweep",
        symbol=sym,
        params={"max_combinations": 32, "min_trades": 3, "bars_lookback": 1500},
        rationale=f"default rotation: parameter sweep on {sym}",
        fallback_used=True,
    )


def _validate_plan_dict(data: Any) -> dict | None:
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    symbol = data.get("symbol")
    params = data.get("params", {})
    if not isinstance(tool, str) or tool not in ALLOWED_TOOLS:
        return None
    if not isinstance(symbol, str) or not symbol:
        return None
    if not isinstance(params, dict):
        return None
    return {
        "tool": tool,
        "symbol": symbol.upper(),
        "params": params,
        "rationale": str(data.get("rationale", "")),
    }


def _build_llm_prompt(context: dict) -> str:
    payload = {"context": context, "allowed_tools": list(ALLOWED_TOOLS)}
    return (
        "You are a paper-futures research planner. Given the context, propose "
        "the single most promising experiment. Respond ONLY with a JSON object "
        'of shape {"tool": "<one_of_allowed_tools>", "symbol": "<TICKER>", '
        '"params": {<tool_kwargs>}, "rationale": "<short>"}. '
        f"Allowed tools: {list(ALLOWED_TOOLS)}.\n\n"
        f"Input: {json.dumps(payload, default=str)}"
    )


def _call_llm(prompt: str) -> str:
    if not os.environ.get("MINIMAX_API_KEY"):
        raise RuntimeError("MINIMAX_API_KEY not set")
    import litellm  # type: ignore[import-not-found]

    resp = litellm.completion(
        model="minimax/MiniMax-M3",
        messages=[{"role": "user", "content": prompt}],
        timeout=_LLM_TIMEOUT_S,
        temperature=0.0,
    )
    return resp["choices"][0]["message"]["content"]  # type: ignore[index]


def _parse_llm_json(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None


def plan_experiment(*, context: dict) -> ExplorePlan:
    """LLM-first; deterministic fallback on any failure."""
    if os.environ.get("MINIMAX_API_KEY"):
        try:
            raw = _call_llm(_build_llm_prompt(context))
            parsed = _parse_llm_json(raw)
            validated = _validate_plan_dict(parsed)
            if validated is not None:
                return ExplorePlan(
                    tool=validated["tool"],
                    symbol=validated["symbol"],
                    params=validated["params"],
                    rationale=validated["rationale"] or "llm plan",
                    fallback_used=False,
                )
            log.warning("explore.plan.llm_invalid_or_disallowed", extra={"raw": str(raw)[:200]})
        except Exception as exc:  # noqa: BLE001
            log.warning("explore.plan.llm_failed", extra={"error": str(exc)})
    return _deterministic_plan(context)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _invoke_research_tool(plan: ExplorePlan) -> dict:
    """Dispatch to the appropriate research_tools entry. Returns dict."""
    from src.tools import research_tools as rt

    tool_name = plan.tool
    payload: dict[str, Any] = {"symbol": plan.symbol, **(plan.params or {})}
    input_json = json.dumps(payload, default=str)

    if tool_name in ("judas_threshold_sweep", "judas_parameter_sweep"):
        # Both names map to judas_parameter_sweep_tool; the underlying tool's
        # experiment_type is "judas_threshold_sweep".
        result_raw = rt.judas_parameter_sweep_tool.run(input_json=input_json)
    elif tool_name == "walk_forward":
        result_raw = rt.walk_forward_tool.run(input_json=input_json)
    elif tool_name in ("pair_sweep", "buffet_zoo_sweep"):
        # Not implemented as discrete tools yet — they require workshop-side
        # battery infrastructure. Surface honestly.
        return {
            "ok": False,
            "experiment_id": None,
            "candidate_id": None,
            "error": f"not_implemented: {tool_name}",
        }
    else:
        return {
            "ok": False,
            "experiment_id": None,
            "candidate_id": None,
            "error": f"unknown_tool: {tool_name}",
        }

    try:
        result = json.loads(result_raw) if isinstance(result_raw, str) else result_raw
    except (TypeError, json.JSONDecodeError):
        result = {}
    if not isinstance(result, dict):
        result = {}

    candidate_id = None
    # walk_forward emits a candidate via _register_judas_candidate; surface it.
    if isinstance(result.get("candidate"), dict):
        candidate_id = result["candidate"].get("candidate_id")

    return {
        "ok": True,
        "experiment_id": result.get("experiment_id"),
        "candidate_id": candidate_id,
        "tool": tool_name,
        "symbol": plan.symbol,
    }


def execute_plan(
    *,
    plan: ExplorePlan,
    db_path: str,
    timeout_s: float = _DEFAULT_EXEC_TIMEOUT_S,
) -> dict:
    """Run the plan with a hard wall-clock timeout. Never raises."""
    # db_path kept on the API surface for parity / future use; the underlying
    # tools resolve it via env / module helpers.
    _ = db_path
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_invoke_research_tool, plan)
            try:
                result = fut.result(timeout=timeout_s)
            except FutureTimeoutError:
                fut.cancel()
                elapsed = time.monotonic() - started
                log.warning(
                    "explore.execute.timeout",
                    extra={"tool": plan.tool, "elapsed_s": elapsed, "timeout_s": timeout_s},
                )
                return {
                    "ok": False,
                    "experiment_id": None,
                    "candidate_id": None,
                    "error": f"timeout after {elapsed:.1f}s (limit {timeout_s}s)",
                }
    except Exception as exc:  # noqa: BLE001
        log.exception("explore.execute.failed", extra={"tool": plan.tool})
        return {
            "ok": False,
            "experiment_id": None,
            "candidate_id": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    log.info(
        "explore.execute.complete",
        extra={
            "tool": plan.tool,
            "symbol": plan.symbol,
            "experiment_id": result.get("experiment_id"),
            "candidate_id": result.get("candidate_id"),
            "ok": result.get("ok"),
        },
    )
    return result
