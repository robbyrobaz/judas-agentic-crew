"""OUT-OF-SAMPLE VALIDATION for the market-entry architecture (2026-10-26).

Runs the EXACT production engine (custom_strategy_runtime.
run_custom_backtest_on_bars) on two disjoint time segments of the deep
archive:

    IS  = first  55% of the archive
    OOS = last   45% of the archive

A config is only worth promoting if it survives BOTH halves.  The previous
cycle's 870-cell grid was scored on the WHOLE archive, so the headline PF of
the best cells is selection-biased; the OOS half is the honest read.

Usage:
  .venv/bin/python research/cycle_20261026/oos_check.py <code.py> <SYMBOL> <tf> <json-grid>
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.research import custom_strategy_runtime as csr  # noqa: E402


def load_bars(symbol: str, tf: str) -> pd.DataFrame:
    p = ROOT / "data" / "bars_archive" / f"{symbol}_{tf}.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts").reset_index(drop=True)


def run_seg(code: str, bars: pd.DataFrame, symbol: str, params: dict) -> dict:
    m = csr.run_custom_backtest_on_bars(
        code=code, bars=bars.reset_index(drop=True), params=params,
        symbol=symbol, qty=1, timeout_s=900,
    )
    return m


def fmt(m: dict) -> str:
    n = m.get("n_trades", 0)
    return (f"PF={m.get('pf', 0):.3f} n={n:4d} "
            f"WR={100.0 * m.get('n_wins', 0) / max(1, n):5.1f}% "
            f"E[R]={m.get('expectancy_r', 0):+.3f} ${m.get('total_pnl', 0):+9.2f} "
            f"dd=${m.get('max_drawdown', 0):8.2f}"
            + (f" ERR={m.get('error')}" if m.get("error") else ""))


def main() -> int:
    code_path = Path(sys.argv[1])
    symbol = sys.argv[2].upper()
    tf = sys.argv[3]
    grid = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
    code = code_path.read_text()

    bars = load_bars(symbol, tf)
    cut = int(len(bars) * 0.55)
    is_bars = bars.iloc[:cut].reset_index(drop=True)
    oos_bars = bars.iloc[cut:].reset_index(drop=True)
    print(f"{symbol} {tf}: {len(bars)} bars {bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")
    print(f"  IS  {len(is_bars)} bars {is_bars['ts'].iloc[0]} -> {is_bars['ts'].iloc[-1]}")
    print(f"  OOS {len(oos_bars)} bars {oos_bars['ts'].iloc[0]} -> {oos_bars['ts'].iloc[-1]}")

    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys])) if keys else [()]
    rows = []
    for combo in combos:
        params = dict(zip(keys, combo))
        mis = run_seg(code, is_bars, symbol, params)
        moos = run_seg(code, oos_bars, symbol, params)
        rows.append((params, mis, moos))
        tag = json.dumps(params)
        print(f"  IS  {tag} {fmt(mis)}", flush=True)
        print(f"  OOS {tag} {fmt(moos)}", flush=True)

    print("\n=== configs that hold up (IS PF>1 and OOS PF>1, OOS n>=8) ===")
    keep = []
    for params, mis, moos in rows:
        if mis.get("pf", 0) > 1.0 and moos.get("pf", 0) > 1.0 and moos.get("n_trades", 0) >= 8:
            keep.append((params, mis, moos))
    for params, mis, moos in sorted(keep, key=lambda r: -r[2].get("pf", 0)):
        print(f"  {json.dumps(params)}\n      IS  {fmt(mis)}\n      OOS {fmt(moos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
