"""Task #2852 part 2 -- the two 'unevidenced row evicted a stamped dollar
earner' cases, plus the #4835 -> #4874 replacement.

Same production path as task2852_supersedure_forensics.py:
run_custom_backtest_on_bars (cost_model=v1_realistic_micros, qty=1, dollar).
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

CONFIGS = [
    # #4863 (active, no dollar pnl) vs #4636 (superseded, total_pnl 6192.85).
    # IDENTICAL params -- pure rename. Same csid, same tf, same every value.
    ("#4863/#4636 MNQ 15m csid271 (identical params, pure rename)", 271, "MNQ", "15m",
     {"sweep_lookback": 5, "atr_period": 14, "body_ratio_min": 0.7,
      "disp_atr_mult": 1.3, "pullback_pct": 0.4, "target_r": 1.5,
      "stop_buffer_atr": 0.1, "qty": 1}),
    # #4867 (active, no dollar pnl) vs #4690 (superseded, total_pnl 5901.52).
    # #4867 additionally DROPPED slot_key + tick.
    ("#4867/#4690 MNQ 5m csid253 (identical params, #4867 dropped slot_key)", 253, "MNQ", "5m",
     {"pivot_len": 5, "lookback": 40, "min_break_atr": 1.0,
      "require_ll_after": True, "require_hh_after": True, "target_r": 2.0,
      "stop_buf_atr": 0.2, "use_equilibrium_entry": True, "atr_period": 14,
      "use_lss": True, "lss_lookback": 10, "lss_min_break": 0.0, "qty": 1}),
    # #4874 replaced #4835 (both csid368 MCL 5m) and stripped slot_key.
    ("#4874 MCL 5m csid368 (replaced #4835, stripped slot_key)", 368, "MCL", "5m",
     {"target_r": 2.0, "stop_buf_ticks": "6", "break_threshold_ticks": "4",
      "max_retest_bars": "24", "rolling_pdhl_bars": "24", "qty": 1}),
]


def main() -> None:
    conn = sqlite3.connect("judas_crew.db")
    out = []
    for label, csid, symbol, tf, params in CONFIGS:
        code = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (csid,)
        ).fetchone()[0]
        bars = pd.read_parquet(f"cache_1h/{symbol}_{tf}.parquet")
        per_day = _BARS_PER_DAY.get(tf, 24)
        if len(bars) > per_day * 90:
            bars = bars.tail(per_day * 90).reset_index(drop=True)
        span = (bars["ts"].iloc[-1] - bars["ts"].iloc[0]).days
        res = run_custom_backtest_on_bars(
            code=code, bars=bars, params=params, symbol=symbol, qty=1,
            timeout_s=600,
        )
        n = res.get("n_trades", 0)
        wr = (res.get("n_wins", 0) / n) if n else 0.0
        rec = {
            "label": label, "csid": csid, "symbol": symbol, "tf": tf,
            "bars": int(len(bars)), "true_span_days": int(span),
            "claimed_days": 90,
            "n_trades": n, "pf": round(float(res.get("pf", 0)), 3),
            "total_pnl": round(float(res.get("total_pnl", 0)), 2),
            "win_rate": round(wr, 3),
            "expectancy_r": round(float(res.get("expectancy_r", 0)), 3),
            "max_dd": round(float(res.get("max_drawdown", 0)), 2),
            "cost_model": res.get("cost_model"), "error": res.get("error"),
            "lure_suspect": bool(float(res.get("pf", 0)) > 3.0 and wr > 0.90),
        }
        out.append(rec)
        print(json.dumps(rec))
    conn.close()
    with open("outputs/research/task2852_forensics_part2.json", "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    main()