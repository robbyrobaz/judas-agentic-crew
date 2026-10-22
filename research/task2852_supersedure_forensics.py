"""Task #2852 -- SUPERSEDURE_FORENSICS_2026-10-20.

Re-backtests the cross-timeframe-superseded rows using the EXACT production
code path (src.research.custom_strategy_runtime.run_custom_backtest_on_bars,
which is what the run_custom_backtest tool calls), so every result carries the
real cost_model=v1_realistic_micros stamp and is dollar-denominated at qty=1.

We deliberately reproduce the tool's bar sourcing: bar_cache parquet + the same
`len(df) > per_day*days` trim. That trim NEVER fires (see finding), so the
window is whatever the cache holds -- we report the true span alongside.

Usage: .venv/bin/python research/task2852_supersedure_forensics.py
"""
from __future__ import annotations

import json
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, '.')

from src.research.custom_strategy_runtime import (  # noqa: E402
    run_custom_backtest_on_bars,
    _BARS_PER_DAY,
)

# (label, csid, symbol, timeframe, params) -- params copied verbatim from the
# disputed active_strategies row so we test the INTENDED config, not defaults.
CONFIGS = [
    # Priority 1: #4905 vs #4912 -- same csid, same symbol, same 5m, differ only
    # in htf_ema_period. #4905 (ema60) was superseded by #4912 (ema20).
    ("#4912  MNQ 5m csid384 htf_ema_period=20 (the survivor)", 384, "MNQ", "5m",
     {"atr_period": 14, "displacement_k": 1.0, "rr": 2.0, "stop_buf_atr": 0.2,
      "ob_age_max": 24, "body_ratio_min": 0.5, "htf_ema_period": 20, "qty": 1}),
    ("#4905  MNQ 5m csid384 htf_ema_period=60 (the evicted one)", 384, "MNQ", "5m",
     {"atr_period": 14, "displacement_k": 1.0, "rr": 2.0, "stop_buf_atr": 0.2,
      "ob_age_max": 24, "body_ratio_min": 0.5, "htf_ema_period": 60, "qty": 1}),
    # Priority 2
    ("#4990  MNQ 15m csid434 (superseded, queued for reinstate)", 434, "MNQ", "15m",
     {"atr_period": 14, "rr": 2.0, "stop_buf_atr": 0.05, "fvg_min_atr": 0.05,
      "fvg_lookback": 6, "min_gap_pct": 0.1, "qty": 1}),
    ("#4991  MNQ 5m csid434 (the survivor)", 434, "MNQ", "5m",
     {"atr_period": 14, "rr": 2.0, "stop_buf_atr": 0.05, "fvg_min_atr": 0.05,
      "fvg_lookback": 6, "min_gap_pct": 0.1, "qty": 1}),
    ("#4784  MGC 15m csid280 bsl_run_judas_eq_cluster (LARGEST kill)", 280, "MGC", "15m",
     {"eq_window": 40, "eq_tol_atr": 0.4, "eq_min_touches": 2, "pivot_len": 2,
      "body_ratio_min": 0.6, "disp_atr_mult": 1.0, "pullback_pct": 0.5,
      "target_r": 1.5, "stop_buffer_atr": 0.1, "atr_period": 14, "qty": 1}),
    ("#4794  MGC 5m csid280 bsl_run_judas_eq_cluster", 280, "MGC", "5m",
     {"eq_window": 40, "eq_tol_atr": 0.4, "eq_min_touches": 2, "pivot_len": 2,
      "body_ratio_min": 0.7, "disp_atr_mult": 1.0, "pullback_pct": 0.5,
      "target_r": 1.5, "stop_buffer_atr": 0.1, "atr_period": 14, "qty": 1}),
    ("#4835  MCL 5m csid368 silver_bullet_nogate_roll24", 368, "MCL", "5m",
     {"target_r": 2.0, "tick": "0.01", "stop_buf_ticks": "6",
      "break_threshold_ticks": "4", "max_retest_bars": "24",
      "rolling_pdhl_bars": "24", "qty": 1}),
]


def main() -> None:
    conn = sqlite3.connect("judas_crew.db")
    out = []
    for label, csid, symbol, tf, params in CONFIGS:
        row = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (csid,)
        ).fetchone()
        conn.close()
        if row is None:
            print(f"!! csid {csid} missing")
            continue
        code = row[0]

        bars = pd.read_parquet(f"cache_1h/{symbol}_{tf}.parquet")
        # EXACT production trim (never fires in practice -- we prove it).
        per_day = _BARS_PER_DAY.get(tf, 24)
        requested = per_day * 90
        trimmed = len(bars) > requested
        if trimmed:
            bars = bars.tail(requested).reset_index(drop=True)
        span_days = (bars["ts"].iloc[-1] - bars["ts"].iloc[0]).days

        res = run_custom_backtest_on_bars(
            code=code, bars=bars, params=params, symbol=symbol, qty=1,
            timeout_s=600,
        )
        n_tr = res.get("n_trades", 0)
        wr = (res.get("n_wins", 0) / n_tr) if n_tr else 0.0
        rec = {
            "label": label,
            "csid": csid, "symbol": symbol, "tf": tf,
            "bars": int(len(bars)),
            "true_span_days": int(span_days),
            "claimed_days": 90,
            "trim_fired": bool(trimmed),
            "n_trades": n_tr, "pf": round(float(res.get("pf", 0)), 3),
            "total_pnl": round(float(res.get("total_pnl", 0)), 2),
            "win_rate": round(wr, 3),
            "expectancy_r": round(float(res.get("expectancy_r", 0)), 3),
            "max_dd": round(float(res.get("max_drawdown", 0)), 2),
            "cost_model": res.get("cost_model"),
            "error": res.get("error"),
            "lure_suspect": bool(
                float(res.get("pf", 0)) > 3.0 and wr > 0.90),
        }
        out.append(rec)
        print(json.dumps(rec))
        conn = sqlite3.connect("judas_crew.db")

    conn.close()
    with open("outputs/research/task2852_supersedure_forensics.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("\nwrote outputs/research/task2852_supersedure_forensics.json")


if __name__ == "__main__":
    main()