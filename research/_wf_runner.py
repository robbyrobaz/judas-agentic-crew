"""Walk-forward: split bars into rolling windows, test on each.

Usage: .venv/bin/python research/_wf_runner.py <csid> <symbol> <tf> <window_bars>
"""
import sys
import json
import sqlite3
import pandas as pd

sys.path.insert(0, '.')

from src.research.custom_strategy_runtime import run_custom_backtest_on_bars


def main():
    csid = int(sys.argv[1])
    symbol = sys.argv[2].upper()
    tf = sys.argv[3]
    window_bars = int(sys.argv[4])
    params = json.loads(sys.argv[5]) if len(sys.argv) > 5 else {}
    n_windows = int(sys.argv[6]) if len(sys.argv) > 6 else 3

    conn = sqlite3.connect('judas_crew.db')
    row = conn.execute(
        "SELECT code FROM custom_strategies WHERE id=?",
        (csid,)
    ).fetchone()
    conn.close()
    code = row[0]

    bars = pd.read_parquet(f'cache_1h/{symbol}_{tf}.parquet').reset_index(drop=True)
    n = len(bars)
    step = (n - window_bars) // max(n_windows - 1, 1)
    print(f"[{symbol} {tf}] total={n}, window={window_bars}, step={step}, n_windows={n_windows}")

    out = []
    for w in range(n_windows):
        start = step * w
        end = start + window_bars
        if end > n: end = n
        sub = bars.iloc[start:end].reset_index(drop=True)
        res = run_custom_backtest_on_bars(code=code, bars=sub, params=params, timeout_s=30)
        out.append(res)
        print(f"  window {w}: start={sub.ts.iloc[0]} end={sub.ts.iloc[-1]} "
              f"n={res['n_signals']} pf={res['pf']:.2f} E[R]={res['expectancy_r']:.2f} "
              f"pnl={res['total_pnl']:.0f} dd={res['max_drawdown']:.0f}")

    # Aggregate
    total_n = sum(r['n_signals'] for r in out)
    total_wins = sum(r['n_wins'] for r in out)
    total_losses = sum(r['n_losses'] for r in out)
    total_pnl = sum(r['total_pnl'] for r in out)
    pos_pnl = sum(r['total_pnl'] for r in out if r['total_pnl'] > 0)
    neg_pnl = -sum(r['total_pnl'] for r in out if r['total_pnl'] < 0)
    avg_pf = sum(r['pf'] for r in out) / len(out)
    print(f"\nAggregate: n={total_n} W={total_wins} L={total_losses} "
          f"pf_combined={pos_pnl/neg_pnl if neg_pnl > 0 else 'inf'} "
          f"avg_pf={avg_pf:.2f} total_pnl={total_pnl:.0f}")


if __name__ == "__main__":
    main()
