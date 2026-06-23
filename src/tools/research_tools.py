"""Deterministic weekend research tools for the exploratory ResearchCrew."""
from __future__ import annotations

import csv
import itertools
import json
import logging
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from crewai.tools import tool

from src.config import load_config
from src.tools.db_tools import db_save_research_experiment_tool
from src.tools.judas_detector import run_judas_detection_rich
from src.strategy_registry import create_candidate, list_active_strategies, promote_candidate

log = logging.getLogger(__name__)


def _workshop_root() -> Path:
    env = Path(
        __import__("os").environ.get(
            "JUDAS_WORKSHOP_PATH",
            str(Path(__file__).parent.parent.parent.parent / "judas-futures-workshop"),
        )
    ).expanduser()
    if not env.exists():
        raise FileNotFoundError(f"Workshop repo not found: {env}")
    return env


def _research_dir() -> Path:
    out = Path(__file__).parent.parent.parent / "outputs" / "research"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _symbol_meta(symbol: str) -> tuple[float, float]:
    cfg = load_config()
    for item in cfg.symbols:
        if item.symbol.upper() == symbol.upper():
            tick = float(item.tick)
            tick_value = tick * float(item.dollar_per_point)
            return tick, tick_value
    _FALLBACK = {
        "MET": (0.50, 0.05),
        "MBT": (5.0, 0.5),
        "DX":  (0.005, 5.0),
        "6J":  (0.0000005, 6.25),
        "MCL": (0.01, 10.0),
        "ZF":  (0.005, 10.0),
    }
    sym = symbol.upper()
    if sym in _FALLBACK:
        t, d = _FALLBACK[sym]; return float(t), float(t * d)
    raise ValueError(f"Unknown symbol {symbol}")


def _latest_timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalize_resample_rule(rule: str) -> str:
    text = str(rule or "").strip()
    if not text:
        return "1h"
    if text[-1].isalpha():
        return f"{text[:-1]}{text[-1].lower()}"
    return text.lower()


def _read_rows(csv_path: Path) -> list[dict[str, Any]]:
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def _top_rows(rows: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    def score(row: dict[str, Any]) -> float:
        try:
            return float(row.get("total_pnl_$", 0.0))
        except Exception:
            return 0.0

    return sorted(rows, key=score, reverse=True)[:limit]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _save_experiment_record(
    *,
    symbol: str,
    experiment_type: str,
    name: str,
    metrics: dict[str, Any],
    parameters: dict[str, Any],
    artifacts: dict[str, Any],
    summary: str,
    recommendations: list[str] | None = None,
) -> int | None:
    payload = {
        "symbol": symbol,
        "experiment_type": experiment_type,
        "name": name,
        "status": "completed",
        "metrics": metrics,
        "parameters": parameters,
        "artifacts": artifacts,
        "summary": summary,
        "recommendations": recommendations or [],
    }
    try:
        saved = json.loads(db_save_research_experiment_tool.run(input_json=json.dumps(payload)))
        return saved.get("experiment_id")
    except Exception:
        return None


def _experiment_catalog() -> dict[str, Any]:
    return {
        "phase": "phase1_judas_1h",
        "focus": "Judas 1H only for active optimization; alt benchmarks remain comparison-only",
        "experiments": [
            {
                "name": "judas_threshold_sweep",
                "description": "Grid-search Judas entry and quality thresholds on 1H cached bars.",
                "parameters": [
                    "target_r",
                    "stop_buffer_ticks",
                    "min_sweep_ticks",
                    "min_displacement_strength",
                    "min_displacement_body_ratio",
                    "max_sweep_age_bars",
                    "require_fvg",
                    "session_filter",
                ],
            },
            {
                "name": "walk_forward",
                "description": "Train/test or rolling validation for a chosen Judas parameter set.",
                "parameters": ["train_ratio", "test_ratio", "step_ratio", "rolling_windows"],
            },
            {
                "name": "alt_strategy_benchmark",
                "description": "Compare current Judas variants against workshop battery winners without auto-activating them.",
                "parameters": ["symbol", "resample"],
            },
            {
                "name": "exit_sweep",
                "description": "Explore Judas exits by varying target R and stop buffer geometry.",
                "parameters": ["target_r", "stop_buffer_ticks"],
            },
        ],
    }


def _strategy_registry() -> dict[str, Any]:
    return {
        "active_research_focus": ["judas_1h"],
        "judas_variants": [
            "judas_base_1h",
            "judas_strict_displacement",
            "judas_fvg_required",
            "judas_session_filtered",
            "judas_tighter_sweep_age",
        ],
        "benchmark_only": [
            "ma_cross",
            "rsi_mean_reversion",
            "bollinger_reversion",
            "donchian_breakout",
        ],
    }


def _load_workshop_leaderboard(limit: int = 20, min_trades: int = 5) -> list[dict[str, Any]]:
    root = _workshop_root()
    path = root / "buffet_top.csv"
    if not path.exists():
        path = root / "buffet_pf_ranked.csv"
    rows = _read_rows(path)
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        trades = _safe_int(row.get("trades"), 0)
        if trades < min_trades:
            continue
        cleaned.append(
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "name": str(row.get("name", "")),
                "kind": str(row.get("kind", "unknown")),
                "trades": trades,
                "winrate": _safe_float(row.get("winrate")),
                "profit_factor": _safe_float(row.get("profit_factor"), _safe_float(row.get("pd_ratio"))),
                "expectancy_r": _safe_float(row.get("expectancy_R"), _safe_float(row.get("avg_R"))),
                "total_pnl_dollars": _safe_float(row.get("total_pnl_$")),
                "max_drawdown_dollars": abs(_safe_float(row.get("max_drawdown_$"))),
                "pd_ratio": _safe_float(row.get("pd_ratio")),
                "sharpe_ish": _safe_float(row.get("sharpe_ish")),
                "raw": row,
            }
        )
    return cleaned[:limit]


def _candidate_decision_from_walk_forward(summary: dict[str, Any]) -> tuple[str, str]:
    window_count = _safe_int(summary.get("window_count"))
    pf = _safe_float(summary.get("avg_test_profit_factor"))
    expectancy = _safe_float(summary.get("avg_test_expectancy_r"))
    max_dd = abs(_safe_float(summary.get("avg_test_max_drawdown_dollars")))
    total_test_trades = _safe_int(summary.get("total_test_trades"))
    zero_trade_windows = _safe_int(summary.get("zero_trade_windows"))

    if (
        window_count >= 3
        and pf >= 1.3
        and expectancy >= 0.15
        and max_dd <= 300.0
        and total_test_trades >= 6
        and zero_trade_windows == 0
    ):
        return "promote_to_paper", "Validated across walk-forward windows with sufficient trade count."

    if window_count >= 2 and pf >= 1.0 and expectancy > 0 and total_test_trades >= 3:
        return "candidate_only", "Interesting but not yet strong enough for automated paper promotion."

    return "reject", "Failed robustness thresholds for automated paper promotion."


def _register_judas_candidate(
    *,
    symbol: str,
    params: dict[str, Any],
    summary: dict[str, Any],
    experiment_id: int | None,
) -> dict[str, Any]:
    decision, rationale = _candidate_decision_from_walk_forward(summary)
    candidate_id = create_candidate(
        symbol=symbol,
        strategy_family="judas_1h",
        params={
            **params,
            "symbol": symbol,
            "strategy_family": "judas_1h",
            "strategy_name": f"judas_auto_{_latest_timestamp_slug()}",
            "execution_engine": "judas_native",
            "timeframe": "1H",
        },
        metrics=summary,
        decision=decision,
        rationale=rationale,
        status="candidate" if decision != "reject" else "rejected",
        source_experiment_id=experiment_id,
    )
    promoted = None
    if decision == "promote_to_paper":
        active = promote_candidate(candidate_id, notes="Automated weekend research promotion to paper.")
        promoted = {
            "active_strategy_id": active.id,
            "version": active.version,
            "activated_at_utc": active.activated_at_utc,
        }
    return {
        "candidate_id": candidate_id,
        "decision": decision,
        "rationale": rationale,
        "promoted": promoted,
    }


@tool("workshop_inventory_tool")
def workshop_inventory_tool(input_json: str = "{}") -> str:
    """List available workshop research assets."""
    _ = input_json
    root = _workshop_root()
    cache_files = sorted(p.name for p in (root / "cache_1h").glob("*.parquet"))
    csv_files = sorted(p.name for p in root.glob("fast_battery*.csv"))
    return json.dumps(
        {
            "workshop_root": str(root),
            "cache_1h_files": cache_files,
            "fast_battery_csvs": csv_files,
            "research_findings_path": str(root / "RESEARCH_FINDINGS.md"),
            "status_path": str(root / "STATUS.md"),
        }
    )


@tool("workshop_findings_tool")
def workshop_findings_tool(input_json: str = "{}") -> str:
    """Return the most relevant workshop findings text for research use."""
    _ = input_json
    root = _workshop_root()
    findings = (root / "RESEARCH_FINDINGS.md").read_text()
    status = (root / "STATUS.md").read_text()
    return json.dumps(
        {
            "research_findings": findings[:16000],
            "status_excerpt": status[:12000],
        }
    )


@tool("experiment_catalog_tool")
def experiment_catalog_tool(input_json: str = "{}") -> str:
    """Return the current research experiment catalog."""
    _ = input_json
    return json.dumps(_experiment_catalog())


@tool("strategy_registry_tool")
def strategy_registry_tool(input_json: str = "{}") -> str:
    """Return the current research strategy registry."""
    _ = input_json
    return json.dumps(
        {
            **_strategy_registry(),
            "active_paper_strategies": list_active_strategies(),
            "workshop_benchmark_top20": _load_workshop_leaderboard(limit=20, min_trades=5),
        }
    )


@tool("workshop_leaderboard_tool")
def workshop_leaderboard_tool(input_json: str = "{}") -> str:
    """Return the top workshop strategy leaderboard rows as benchmark candidates."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    limit = _safe_int(data.get("limit", 20), 20)
    min_trades = _safe_int(data.get("min_trades", 5), 5)
    return json.dumps(
        {
            "top_strategies": _load_workshop_leaderboard(limit=limit, min_trades=min_trades),
            "source_csv": str((_workshop_root() / "buffet_top.csv")),
        }
    )


@tool("workshop_fast_battery_tool")
def workshop_fast_battery_tool(input_json: str) -> str:
    """Run the workshop fast battery deterministically on cached bars."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    symbol = str(data.get("symbol", "MGC")).upper()
    requested_resample = str(data.get("resample") or data.get("timeframe") or "1H")
    resample = _normalize_resample_rule(requested_resample)
    root = _workshop_root()
    tick, tick_value = _symbol_meta(symbol)
    parquet = Path(data.get("parquet") or root / "cache_1h" / f"{symbol}_1h.parquet")
    if not parquet.exists():
        raise FileNotFoundError(f"Parquet not found: {parquet}")

    out_csv = _research_dir() / f"fast_battery_{symbol}_{_latest_timestamp_slug()}.csv"
    py = root / ".venv" / "bin" / "python"
    script = root / "scripts" / "fast_battery.py"
    cmd = [
        str(py),
        str(script),
        "--parquet",
        str(parquet),
        "--symbol",
        symbol,
        "--tick",
        str(tick),
        "--tick-value",
        str(tick_value),
        "--resample",
        resample,
        "--out",
        str(out_csv),
    ]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True)
    rows = _read_rows(out_csv)
    top = _top_rows(rows, limit=10)
    return json.dumps(
        {
            "symbol": symbol,
            "requested_resample": requested_resample,
            "resample": resample,
            "parquet": str(parquet),
            "output_csv": str(out_csv),
            "row_count": len(rows),
            "top_results": top,
            "stdout_tail": proc.stdout[-6000:],
        }
    )


@dataclass
class JudasTrade:
    signal_ts: str
    direction: str
    entry: float
    stop: float
    target: float
    exit_ts: str | None
    exit_price: float | None
    exit_reason: str | None
    pnl_r: float | None
    pnl_dollars: float | None
    displacement_strength: float
    displacement_body_ratio: float
    sweep_type: str
    sweep_age_bars: int


def _load_bars(symbol: str) -> pd.DataFrame:
    # Read THIS repo's bar cache (same source as the live scanner /
    # custom_strategy_runtime), NOT the workshop repo's cache_1h. The workshop
    # cache is refreshed by a separate project and was found 7 weeks stale
    # (last bar 2026-05-05) on 2026-06-22 — so judas_native backtests were
    # validating strategies on data that never saw recent regime, then losing
    # live. bar_cache.read_cache reads cache_1h/<SYM>_1h and is kept fresh.
    from src.bar_cache import read_cache

    bars = read_cache(symbol, "1h")
    return bars.sort_values("ts").reset_index(drop=True)


def _session_name_from_et(ts: pd.Timestamp) -> str | None:
    et = ts.tz_convert("America/New_York")
    hhmm = et.hour * 60 + et.minute
    if 9 * 60 + 30 <= hhmm <= 11 * 60 + 30:
        return "ny_open"
    if 14 * 60 <= hhmm <= 15 * 60 + 30:
        return "power_hour"
    if 2 * 60 <= hhmm <= 5 * 60:
        return "london"
    return None


def _passes_session_filter(ts: pd.Timestamp, session_filter: str) -> bool:
    if not session_filter or session_filter == "all":
        return True
    return _session_name_from_et(ts) == session_filter


def _recovery_bars(pnls: list[float]) -> int | None:
    if not pnls:
        return None
    equity = 0.0
    peak = 0.0
    drawdown_start: int | None = None
    best_recovery: int | None = None
    for idx, pnl in enumerate(pnls):
        equity += pnl
        if equity >= peak:
            peak = equity
            if drawdown_start is not None:
                recovery = idx - drawdown_start
                best_recovery = recovery if best_recovery is None else min(best_recovery, recovery)
                drawdown_start = None
            continue
        if drawdown_start is None:
            drawdown_start = idx
    return best_recovery


def _summarize_pnls(pnls: list[float], rs: list[float], wins: int, losses: int, skipped: int) -> dict[str, Any]:
    total = round(sum(pnls), 2)
    gross_win = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    trade_count = wins + losses
    winrate = round(wins / (wins + losses), 3) if (wins + losses) else 0.0
    avg_r = round(sum(rs) / len(rs), 3) if rs else 0.0
    expectancy_r = avg_r
    if trade_count == 0:
        profit_factor = 0.0
    elif gross_loss > 0:
        profit_factor = round(gross_win / gross_loss, 3)
    else:
        profit_factor = 5.0

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    mean_r = sum(rs) / len(rs) if rs else 0.0
    std_r = pd.Series(rs).std(ddof=0) if len(rs) > 1 else 0.0
    sharpe = round((mean_r / std_r) * (252 ** 0.5), 3) if len(rs) >= 3 and std_r and std_r > 1e-9 else 0.0
    recovery = _recovery_bars(pnls)
    return {
        "trades": trade_count,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "profit_factor": profit_factor,
        "avg_r": avg_r,
        "expectancy_r": expectancy_r,
        "total_pnl_dollars": total,
        "max_drawdown_dollars": round(max_dd, 2),
        "sharpe_ish": sharpe,
        "recovery_trades": recovery,
        "skipped_signals": skipped,
        "a_plus_trade_count": 0,
        "a_plus_winrate": 0.0,
    }


def _rank_score(metrics: dict[str, Any]) -> float:
    pf = min(_safe_float(metrics.get("profit_factor")), 5.0)
    expectancy = _safe_float(metrics.get("expectancy_r"))
    winrate = _safe_float(metrics.get("winrate"))
    sharpe = _safe_float(metrics.get("sharpe_ish"))
    pnl = _safe_float(metrics.get("total_pnl_dollars"))
    max_dd = abs(_safe_float(metrics.get("max_drawdown_dollars")))
    a_plus_winrate = _safe_float(metrics.get("a_plus_winrate"))
    recovery = _safe_float(metrics.get("recovery_trades"), 999.0)
    trades = _safe_float(metrics.get("trades"))
    trade_quality_bonus = min(trades / 10.0, 3.0)
    low_sample_penalty = max(0.0, (5.0 - trades) * 2.5)
    dd_penalty = max_dd / 400.0
    recovery_penalty = 0.0 if recovery <= 0 else min(recovery / 10.0, 3.0)
    pnl_bonus = pnl / 1000.0
    return round(
        (pf * 2.5)
        + (expectancy * 4.0)
        + (winrate * 2.0)
        + (a_plus_winrate * 1.5)
        + (sharpe * 0.35)
        + pnl_bonus
        + trade_quality_bonus
        - dd_penalty
        - recovery_penalty,
        4,
    ) - round(low_sample_penalty, 4)


def _passes_minimum_trade_quality(metrics: dict[str, Any], min_trades: int) -> bool:
    return _safe_int(metrics.get("trades")) >= max(min_trades, 1)


def _evaluate_judas_variant(
    bars: pd.DataFrame,
    symbol: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    tick, tick_value = _symbol_meta(symbol)
    trades: list[dict[str, Any]] = []
    pnls: list[float] = []
    rs: list[float] = []
    seen_signal_ts: set[str] = set()
    skipped = 0
    wins = 0
    losses = 0
    a_plus_wins = 0
    a_plus_losses = 0

    detector_lookback_bars = _safe_int(params.get("detector_lookback_bars", 120), 120)

    for idx in range(40, len(bars)):
        levels = _prior_day_levels(bars, idx)
        if not levels:
            continue
        prior_high, prior_low = levels
        window_start = max(0, idx + 1 - detector_lookback_bars)
        window = bars.iloc[window_start : idx + 1].copy()
        detector = run_judas_detection_rich(
            symbol=symbol.upper(),
            bars_df=window,
            prior_high=prior_high,
            prior_low=prior_low,
            tick_size=tick,
            confirmation_bars=int(params.get("confirmation_bars", 4)),
            pivot_length=int(params.get("pivot_length", 2)),
            target_r=float(params.get("target_r", 2.0)),
            stop_buffer_ticks=int(params.get("stop_buffer_ticks", 2)),
            min_sweep_ticks=int(params.get("min_sweep_ticks", 3)),
            dollar_per_point=tick_value / tick if tick > 0 else 10.0,
        )
        if not detector.get("pattern_found"):
            continue

        signal_ts = pd.to_datetime(window.iloc[-1]["ts"], utc=True)
        signal_key = signal_ts.isoformat()
        if signal_key in seen_signal_ts:
            continue
        seen_signal_ts.add(signal_key)

        displacement = detector.get("displacement") or {}
        atr_context = detector.get("atr_context") or {}
        choch = detector.get("choch") or {}
        sweep = detector.get("sweep") or {}
        fvg = detector.get("fvg") or {}

        sweep_age = int((choch.get("bar_idx") or 0) - (sweep.get("bar_idx") or 0))
        disp_strength = _safe_float(displacement.get("strength"))
        body_ratio = _safe_float(displacement.get("body_ratio"))
        current_atr = _safe_float(atr_context.get("current_atr"))
        avg_atr = _safe_float(atr_context.get("avg_atr_20"))
        atr_ratio = (current_atr / avg_atr) if avg_atr > 0 else 0.0

        if disp_strength < _safe_float(params.get("min_displacement_strength", 0.0)):
            skipped += 1
            continue
        if body_ratio < _safe_float(params.get("min_displacement_body_ratio", 0.0)):
            skipped += 1
            continue
        if sweep_age > _safe_int(params.get("max_sweep_age_bars", 99)):
            skipped += 1
            continue
        if atr_ratio < _safe_float(params.get("min_atr_ratio", 0.0)):
            skipped += 1
            continue
        if bool(params.get("require_fvg", False)) and not bool(fvg.get("present")):
            skipped += 1
            continue
        session_filter = str(params.get("session_filter", "all"))
        if not _passes_session_filter(signal_ts, session_filter):
            skipped += 1
            continue

        direction = str(detector["direction"])
        entry = _safe_float(choch.get("entry_price"))
        stop = _safe_float(detector.get("stop_price"))
        target = _safe_float(detector.get("target_price"))
        risk = abs(entry - stop)
        exit_ts, exit_price, exit_reason = _simulate_exit(
            bars=bars,
            start_idx=idx,
            direction=direction,
            stop=stop,
            target=target,
        )
        if exit_price is None or risk <= 0:
            continue

        pnl_r = ((exit_price - entry) / risk) if direction == "long" else ((entry - exit_price) / risk)
        pnl_dollars = pnl_r * _safe_float(detector.get("risk_dollars"))
        pnls.append(pnl_dollars)
        rs.append(pnl_r)

        is_a_plus = (
            disp_strength >= max(_safe_float(params.get("min_displacement_strength", 0.0)), 1.8)
            and body_ratio >= max(_safe_float(params.get("min_displacement_body_ratio", 0.0)), 0.55)
            and sweep_age <= 1
        )
        if pnl_dollars > 0:
            wins += 1
            if is_a_plus:
                a_plus_wins += 1
        else:
            losses += 1
            if is_a_plus:
                a_plus_losses += 1

        trades.append(
            {
                "signal_ts": signal_key,
                "direction": direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "exit_ts": exit_ts,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl_r": round(pnl_r, 4),
                "pnl_dollars": round(pnl_dollars, 2),
                "displacement_strength": round(disp_strength, 3),
                "displacement_body_ratio": round(body_ratio, 3),
                "atr_ratio": round(atr_ratio, 3),
                "sweep_age_bars": sweep_age,
                "session": _session_name_from_et(signal_ts),
                "a_plus": is_a_plus,
            }
        )

    metrics = _summarize_pnls(pnls, rs, wins, losses, skipped)
    a_plus_total = a_plus_wins + a_plus_losses
    metrics["a_plus_trade_count"] = a_plus_total
    metrics["a_plus_winrate"] = round(a_plus_wins / a_plus_total, 3) if a_plus_total else 0.0
    metrics["rank_score"] = _rank_score(metrics)
    return {"metrics": metrics, "trades": trades}


def _prior_day_levels(bars: pd.DataFrame, current_idx: int) -> tuple[float, float] | None:
    current_date = str(pd.to_datetime(bars.iloc[current_idx]["ts"], utc=True).date())
    history = bars.iloc[:current_idx].copy()
    if history.empty:
        return None
    history["session_date"] = pd.to_datetime(history["ts"], utc=True).dt.date.astype(str)
    prior_rows = history[history["session_date"] < current_date]
    if prior_rows.empty:
        return None
    prior_date = prior_rows["session_date"].max()
    day = prior_rows[prior_rows["session_date"] == prior_date]
    return float(day["high"].max()), float(day["low"].min())


def _simulate_exit(
    bars: pd.DataFrame,
    start_idx: int,
    direction: str,
    stop: float,
    target: float,
) -> tuple[str | None, float | None, str | None]:
    for idx in range(start_idx + 1, len(bars)):
        row = bars.iloc[idx]
        hi = float(row["high"])
        lo = float(row["low"])
        ts = str(row["ts"])
        if direction == "long":
            if lo <= stop:
                return ts, stop, "stop"
            if hi >= target:
                return ts, target, "target"
        else:
            if hi >= stop:
                return ts, stop, "stop"
            if lo <= target:
                return ts, target, "target"
    return None, None, None


def run_judas_historical_backtest(symbol: str = "MGC") -> dict[str, Any]:
    bars = _load_bars(symbol)
    from src.bar_cache import cache_path
    parquet = cache_path(symbol, "1h")  # provenance: matches _load_bars' real source
    tick, tick_value = _symbol_meta(symbol)
    detector_lookback_bars = 120

    trades: list[JudasTrade] = []
    seen_signal_ts: set[str] = set()
    for idx in range(40, len(bars)):
        levels = _prior_day_levels(bars, idx)
        if not levels:
            continue
        prior_high, prior_low = levels
        window_start = max(0, idx + 1 - detector_lookback_bars)
        window = bars.iloc[window_start : idx + 1].copy()
        detector = run_judas_detection_rich(
            symbol=symbol.upper(),
            bars_df=window,
            prior_high=prior_high,
            prior_low=prior_low,
            tick_size=tick,
            dollar_per_point=tick_value / tick if tick > 0 else 10.0,
        )
        if not detector.get("pattern_found"):
            continue

        signal_ts = str(window.iloc[-1]["ts"])
        if signal_ts in seen_signal_ts:
            continue
        seen_signal_ts.add(signal_ts)

        direction = str(detector["direction"])
        entry = float(detector["choch"]["entry_price"])
        stop = float(detector["stop_price"])
        target = float(detector["target_price"])
        risk = abs(entry - stop)
        exit_ts, exit_price, exit_reason = _simulate_exit(
            bars=bars,
            start_idx=idx,
            direction=direction,
            stop=stop,
            target=target,
        )
        pnl_r = None
        pnl_dollars = None
        if exit_price is not None and risk > 0:
            if direction == "long":
                pnl_r = (exit_price - entry) / risk
            else:
                pnl_r = (entry - exit_price) / risk
            pnl_dollars = pnl_r * float(detector["risk_dollars"])

        sweep = detector.get("sweep") or {}
        choch = detector.get("choch") or {}
        trades.append(
            JudasTrade(
                signal_ts=signal_ts,
                direction=direction,
                entry=entry,
                stop=stop,
                target=target,
                exit_ts=exit_ts,
                exit_price=exit_price,
                exit_reason=exit_reason,
                pnl_r=pnl_r,
                pnl_dollars=pnl_dollars,
                displacement_strength=float((detector.get("displacement") or {}).get("strength") or 0.0),
                displacement_body_ratio=float((detector.get("displacement") or {}).get("body_ratio") or 0.0),
                sweep_type=str(sweep.get("type") or ""),
                sweep_age_bars=int((choch.get("bar_idx") or 0) - (sweep.get("bar_idx") or 0)),
            )
        )

    closed = [t for t in trades if t.exit_reason]
    wins = [t for t in closed if (t.pnl_r or 0.0) > 0]
    avg_r = sum((t.pnl_r or 0.0) for t in closed) / len(closed) if closed else 0.0
    summary = {
        "symbol": symbol.upper(),
        "parquet": str(parquet),
        "signal_count": len(trades),
        "closed_trade_count": len(closed),
        "win_count": len(wins),
        "winrate": round(len(wins) / len(closed), 3) if closed else 0.0,
        "avg_r": round(avg_r, 3),
        "total_pnl_dollars": round(sum((t.pnl_dollars or 0.0) for t in closed), 2),
        "avg_displacement_strength": round(
            sum(t.displacement_strength for t in trades) / len(trades), 3
        ) if trades else 0.0,
        "avg_body_ratio": round(
            sum(t.displacement_body_ratio for t in trades) / len(trades), 3
        ) if trades else 0.0,
        "wick_sweep_ratio": round(
            sum(1 for t in trades if t.sweep_type == "wick") / len(trades), 3
        ) if trades else 0.0,
    }
    return {
        "summary": summary,
        "trades": [asdict(t) for t in trades],
    }


@tool("judas_backtest_tool")
def judas_backtest_tool(input_json: str) -> str:
    """Run a deterministic Judas 1H historical backtest on workshop cache data."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    symbol = str(data.get("symbol", "MGC")).upper()
    result = run_judas_historical_backtest(symbol=symbol)
    out_dir = _research_dir()
    slug = _latest_timestamp_slug()
    trades_csv = out_dir / f"judas_backtest_{symbol}_{slug}.csv"
    summary_json = out_dir / f"judas_backtest_{symbol}_{slug}.json"
    trades = result["trades"]
    if trades:
        with open(trades_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
            writer.writeheader()
            writer.writerows(trades)
    summary_json.write_text(json.dumps(result, indent=2))
    return json.dumps(
        {
            "summary": result["summary"],
            "trades_csv": str(trades_csv),
            "summary_json": str(summary_json),
        }
    )


@tool("judas_parameter_sweep_tool")
def judas_parameter_sweep_tool(input_json: str) -> str:
    """Run a deterministic Judas 1H parameter sweep and rank the results.

    Pass an explicit `grid` dict to override the trimmed default. Use
    `max_combinations` (default 64) to cap the cartesian product — sweeps
    that would exceed the cap are truncated and a warning is logged so the
    tool always returns within a bounded wall-clock budget.

    Set `bars_lookback` to limit the bar slice (default = full cached bars)
    for fast tests. Set `db_path` (or `JUDAS_DB_PATH` env) to redirect the
    research_experiments write target.
    """
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    symbol = str(data.get("symbol", "MGC")).upper()
    bars = _load_bars(symbol)
    bars_lookback = _safe_int(data.get("bars_lookback", 0), 0)
    if bars_lookback and bars_lookback > 0:
        bars = bars.tail(bars_lookback).reset_index(drop=True)
    # Trimmed default: completes in a few minutes on a 60D MGC slice.
    # The 2026-05-09 timeout was caused by a 1728-combo default grid that
    # could not finish within the 45-min timer, so zero rows were ever
    # persisted. The explicit `grid` argument is still available for
    # operator-driven exhaustive sweeps.
    default_grid = {
        "target_r": [1.5, 2.0],
        "stop_buffer_ticks": [2],
        "min_sweep_ticks": [3],
        "min_displacement_strength": [1.0, 1.5],
        "min_displacement_body_ratio": [0.5],
        "max_sweep_age_bars": [2],
        "require_fvg": [False],
        "session_filter": ["all", "ny_open"],
    }
    grid = data.get("grid", default_grid)
    min_trades = _safe_int(data.get("min_trades", 3), 3)
    max_combinations = _safe_int(data.get("max_combinations", 64), 64)
    keys = list(grid.keys())
    values = [list(grid[k]) for k in keys]
    total_combos = 1
    for v in values:
        total_combos *= max(len(v), 1)
    truncated = False
    if max_combinations > 0 and total_combos > max_combinations:
        truncated = True
        log.warning(
            "judas_parameter_sweep: grid %d combos exceeds max_combinations=%d; truncating",
            total_combos,
            max_combinations,
        )

    experiments: list[dict[str, Any]] = []
    evaluated = 0
    for combo in itertools.product(*values):
        if max_combinations > 0 and evaluated >= max_combinations:
            break
        evaluated += 1
        params = dict(zip(keys, combo, strict=False))
        result = _evaluate_judas_variant(bars, symbol, params)
        metrics = result["metrics"]
        if not _passes_minimum_trade_quality(metrics, min_trades):
            continue
        experiments.append(
            {
                "symbol": symbol,
                "name": "judas_threshold_sweep",
                "parameters": params,
                "metrics": metrics,
                "artifacts": {},
            }
        )

    ranked = sorted(experiments, key=lambda x: x["metrics"]["rank_score"], reverse=True)
    top_n = _safe_int(data.get("top_n", 10), 10)
    top_ranked = ranked[:top_n]

    out_dir = _research_dir()
    slug = _latest_timestamp_slug()
    json_path = out_dir / f"judas_threshold_sweep_{symbol}_{slug}.json"
    csv_path = out_dir / f"judas_threshold_sweep_{symbol}_{slug}.csv"
    json_path.write_text(json.dumps({"symbol": symbol, "experiments": ranked}, indent=2))
    with open(csv_path, "w", newline="") as f:
        fieldnames = [
            "rank_score", "profit_factor", "expectancy_r", "winrate", "a_plus_winrate",
            "sharpe_ish", "max_drawdown_dollars", "total_pnl_dollars", "trades",
            *keys,
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for item in ranked:
            row = {**item["metrics"], **item["parameters"]}
            writer.writerow({name: row.get(name) for name in fieldnames})

    experiment_id = _save_experiment_record(
        symbol=symbol,
        experiment_type="judas_threshold_sweep",
        name=f"{symbol} Judas threshold sweep",
        metrics={"top_rank_score": top_ranked[0]["metrics"]["rank_score"] if top_ranked else 0.0, "experiment_count": len(ranked)},
        parameters={"grid": grid},
        artifacts={"json_path": str(json_path), "csv_path": str(csv_path)},
        summary=f"Ranked {len(ranked)} Judas 1H parameter combinations for {symbol}.",
        recommendations=[
            "Review top-ranked configurations before promoting any rule to the live trading crew.",
            "Use walk-forward validation on the top sweep results before trusting them.",
        ],
    )
    return json.dumps(
        {
            "symbol": symbol,
            "experiment_count": len(ranked),
            "evaluated_combinations": evaluated,
            "total_grid_combinations": total_combos,
            "truncated": truncated,
            "top_results": top_ranked,
            "json_path": str(json_path),
            "csv_path": str(csv_path),
            "experiment_id": experiment_id,
        }
    )


@tool("walk_forward_tool")
def walk_forward_tool(input_json: str) -> str:
    """Run rolling walk-forward validation for a single Judas parameter set."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    symbol = str(data.get("symbol", "MGC")).upper()
    params = dict(data.get("parameters", {}))
    bars = _load_bars(symbol)
    rolling_windows = _safe_int(data.get("rolling_windows", 3), 3)
    train_ratio = _safe_float(data.get("train_ratio", 0.6), 0.6)
    test_ratio = _safe_float(data.get("test_ratio", 0.2), 0.2)
    total = len(bars)
    train_size = max(int(total * train_ratio), 100)
    test_size = max(int(total * test_ratio), 40)
    step = max((total - train_size - test_size) // max(rolling_windows - 1, 1), 1)

    windows: list[dict[str, Any]] = []
    for w in range(rolling_windows):
        start = w * step
        train_end = start + train_size
        test_end = train_end + test_size
        if test_end > total:
            break
        train_result = _evaluate_judas_variant(bars.iloc[start:train_end].reset_index(drop=True), symbol, params)
        test_result = _evaluate_judas_variant(bars.iloc[train_end:test_end].reset_index(drop=True), symbol, params)
        windows.append(
            {
                "window": w + 1,
                "train_range": [str(bars.iloc[start]["ts"]), str(bars.iloc[train_end - 1]["ts"])],
                "test_range": [str(bars.iloc[train_end]["ts"]), str(bars.iloc[test_end - 1]["ts"])],
                "train_metrics": train_result["metrics"],
                "test_metrics": test_result["metrics"],
            }
        )

    avg_test_pf = sum(_safe_float(w["test_metrics"]["profit_factor"]) for w in windows) / len(windows) if windows else 0.0
    avg_test_expectancy = sum(_safe_float(w["test_metrics"]["expectancy_r"]) for w in windows) / len(windows) if windows else 0.0
    avg_test_dd = sum(abs(_safe_float(w["test_metrics"]["max_drawdown_dollars"])) for w in windows) / len(windows) if windows else 0.0
    total_test_trades = sum(_safe_int(w["test_metrics"].get("trades"), 0) for w in windows)
    zero_trade_windows = sum(1 for w in windows if _safe_int(w["test_metrics"].get("trades"), 0) == 0)
    robustness_score = round((avg_test_pf * 2.0) + (avg_test_expectancy * 4.0) - (avg_test_dd / 500.0), 4)

    out_dir = _research_dir()
    slug = _latest_timestamp_slug()
    json_path = out_dir / f"walk_forward_{symbol}_{slug}.json"
    payload = {
        "symbol": symbol,
        "parameters": params,
        "windows": windows,
        "summary": {
            "window_count": len(windows),
            "avg_test_profit_factor": round(avg_test_pf, 3),
            "avg_test_expectancy_r": round(avg_test_expectancy, 3),
            "avg_test_max_drawdown_dollars": round(avg_test_dd, 2),
            "total_test_trades": total_test_trades,
            "zero_trade_windows": zero_trade_windows,
            "robustness_score": robustness_score,
        },
    }
    json_path.write_text(json.dumps(payload, indent=2))
    experiment_id = _save_experiment_record(
        symbol=symbol,
        experiment_type="walk_forward",
        name=f"{symbol} Judas walk-forward",
        metrics=payload["summary"],
        parameters=params,
        artifacts={"json_path": str(json_path)},
        summary=f"Walk-forward validation across {len(windows)} windows for {symbol} Judas 1H.",
        recommendations=[
            "Prefer sweep winners that also hold up in walk-forward validation.",
        ],
    )
    payload["experiment_id"] = experiment_id
    payload["candidate_action"] = _register_judas_candidate(
        symbol=symbol,
        params=params,
        summary=payload["summary"],
        experiment_id=experiment_id,
    )
    payload["json_path"] = str(json_path)
    return json.dumps(payload)


@tool("experiment_ranker_tool")
def experiment_ranker_tool(input_json: str) -> str:
    """Rank a list of experiment results with the same multi-objective score used by the lab."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    experiments = list(data.get("experiments", []))
    for item in experiments:
        metrics = item.get("metrics", {})
        metrics["rank_score"] = _rank_score(metrics)
    ranked = sorted(experiments, key=lambda x: _safe_float((x.get("metrics") or {}).get("rank_score")), reverse=True)
    return json.dumps({"ranked_experiments": ranked})


@tool("save_research_report_tool")
def save_research_report_tool(input_json: str) -> str:
    """Persist a final research report to outputs/research as JSON and Markdown."""
    data = json.loads(input_json) if isinstance(input_json, str) else input_json
    slug = _latest_timestamp_slug()
    out_dir = _research_dir()
    json_path = out_dir / f"report_{slug}.json"
    md_path = out_dir / f"report_{slug}.md"
    json_path.write_text(json.dumps(data, indent=2))

    sections = [
        "# Research Report",
        "",
        f"- generated_at_utc: {slug}",
        f"- title: {data.get('title', 'Weekend research run')}",
        "",
        "## Summary",
        str(data.get("summary", "")),
        "",
        "## Recommendations",
    ]
    for item in data.get("recommendations", []):
        sections.append(f"- {item}")
    sections.extend(["", "## Evidence", json.dumps(data.get("evidence", {}), indent=2)])
    md_path.write_text("\n".join(sections))
    experiment_id = None
    experiment_payload = {
        "symbol": data.get("symbol", "MGC"),
        "experiment_type": data.get("experiment_type", "research_report"),
        "name": data.get("title", "Weekend research run"),
        "status": data.get("status", "completed"),
        "metrics": data.get("metrics", {}),
        "parameters": data.get("parameters", {}),
        "artifacts": {
            "json_path": str(json_path),
            "md_path": str(md_path),
            **(data.get("evidence") or {}),
        },
        "summary": data.get("summary", ""),
        "recommendations": data.get("recommendations", []),
    }
    try:
        saved = json.loads(db_save_research_experiment_tool.run(input_json=json.dumps(experiment_payload)))
        experiment_id = saved.get("experiment_id")
    except Exception:
        experiment_id = None
    return json.dumps({"json_path": str(json_path), "md_path": str(md_path), "experiment_id": experiment_id})
