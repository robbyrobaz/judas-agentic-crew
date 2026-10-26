"""Local exploration harness for the Researcher.

Runs the EXACT production backtest engine (custom_strategy_runtime.
run_custom_backtest_on_bars) over the deep bar archive so parameter
exploration is fast and free. Final candidates are re-confirmed through the
official run_custom_backtest tool so the stamp comes from the sanctioned path.

Usage:
  .venv/bin/python research/tmp_harness.py <code_file.py> <SYMBOL> <tf> <days>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.research import custom_strategy_runtime as csr  # noqa: E402


def load_bars(symbol: str, tf: str, days: int) -> pd.DataFrame:
    p = Path("data/bars_archive") / f"{symbol}_{tf}.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").reset_index(drop=True)
    per_day = {"5m": 288, "15m": 96, "1h": 24}[tf]
    if days and len(df) > per_day * days:
        df = df.tail(per_day * days).reset_index(drop=True)
    return df


def run(code: str, symbol: str, tf: str, days: int, params: dict | None = None) -> dict:
    bars = load_bars(symbol, tf, days)
    t0 = time.time()
    m = csr.run_custom_backtest_on_bars(
        code=code, bars=bars, params=params or {}, symbol=symbol, qty=1, timeout_s=600
    )
    m["wall_s"] = round(time.time() - t0, 2)
    m["span"] = f"{bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}"
    return m


def fmt(m: dict) -> str:
    return (
        f"PF={m.get('pf', 0):.3f} n={m.get('n_trades', 0):3d} "
        f"WR={100.0 * m.get('n_wins', 0) / max(1, m.get('n_trades', 0)):5.1f}% "
        f"E[R]={m.get('expectancy_r', 0):+.3f} ${m.get('total_pnl', 0):+.2f} "
        f"DD=${m.get('max_drawdown', 0):.2f} stale={m.get('stale_fill_pct', 0)}% "
        f"valid={m.get('backtest_fill_valid')} err={m.get('error')}"
    )


if __name__ == "__main__":
    code_path, symbol, tf, days = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    code = Path(code_path).read_text()
    params = {}
    if len(sys.argv) > 5 and sys.argv[5]:
        import json
        params = json.loads(sys.argv[5])
    print(f"{symbol} {tf} {days}d params={params}")
    print(fmt(run(code, symbol, tf, days, params)))
