"""Run the PRODUCTION house engine (custom_strategy_runtime) over a date window.

This is the same accounting the sanctioned `run_custom_backtest` tool uses
(cost_model=v1_realistic_micros, qty=1, dollar-denominated), so the numbers
here are directly comparable with the v1 stamp -- but the WINDOW is chosen by
the caller, which the tool does not allow (it always takes the most recent
`days` of the merged archive).

Usage:
  .venv/bin/python research/cycle_20261026/house_window.py \
      <code.py> <SYMBOL> <tf> <start_ts> <end_ts>
  (end_ts may be 'end')
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402


def main() -> int:
    code_path = Path(sys.argv[1])
    symbol = sys.argv[2].upper()
    tf = sys.argv[3]
    start = sys.argv[4]
    end = sys.argv[5] if len(sys.argv) > 5 else "end"
    code = code_path.read_text()

    df = bar_archive.read_archive(symbol, tf)
    if df is None or len(df) == 0:
        print("no archive")
        return 1
    ts = pd_ts(df)
    seg = df[(ts >= start) & (ts <= end)].reset_index(drop=True)
    print(f"{symbol} {tf} window {start} -> {end}: {len(seg)} bars "
          f"{seg['ts'].iloc[0]} -> {seg['ts'].iloc[-1]}")
    m = csr.run_custom_backtest_on_bars(code=code, bars=seg, params={},
                                         symbol=symbol, qty=1, timeout_s=900)
    keys = ("n_trades", "n_wins", "n_losses", "pf", "expectancy_r", "total_pnl",
            "max_drawdown", "cost_model", "qty", "error", "n_bars",
            "bars_span_days", "first_bar", "last_bar", "stale_fill_n",
            "backtest_fill_valid")
    print(json.dumps({k: m.get(k) for k in keys}, indent=2, default=str))
    return 0


def pd_ts(df):
    import pandas as pd
    return pd.to_datetime(df["ts"])


if __name__ == "__main__":
    sys.exit(main())
