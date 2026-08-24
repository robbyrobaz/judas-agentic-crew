"""Local helper: run custom_strategy evaluate() on parquet cache bars.

Usage: .venv/bin/python research/_local_bt_runner.py <csid> <symbol> <tf> <days>
"""
import sys
import json
import sqlite3
import pandas as pd

# Force the runtime imports to use the same namespace shape as custom_strategy_runtime
sys.path.insert(0, '.')

from src.research.custom_strategy_runtime import run_custom_backtest_on_bars


def main():
    csid = int(sys.argv[1])
    symbol = sys.argv[2].upper()
    tf = sys.argv[3]
    days = int(sys.argv[4]) if len(sys.argv) > 4 else 60
    params = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}

    conn = sqlite3.connect('judas_crew.db')
    row = conn.execute(
        "SELECT code, backtest_metrics_json FROM custom_strategies WHERE id=?",
        (csid,)
    ).fetchone()
    conn.close()
    code = row[0]
    print(f"[csid {csid}] code length {len(code)}")

    bars = pd.read_parquet(f'cache_1h/{symbol}_{tf}.parquet')
    # Trim to `days` trading days based on bars-per-day
    per_day = {"5m": 288, "15m": 96, "1h": 24}[tf]
    if len(bars) > per_day * days:
        bars = bars.tail(per_day * days).reset_index(drop=True)
    print(f"[{symbol} {tf}] bars={len(bars)} range={bars.ts.iloc[0]} -> {bars.ts.iloc[-1]}")

    res = run_custom_backtest_on_bars(code=code, bars=bars, params=params, timeout_s=60)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
