"""Validate explore_prod.simulate_prod + sigs_sweep_disp against the REAL engine.

Runs csid439's own code through src.research.custom_strategy_runtime.
run_custom_backtest_on_bars (PRODUCTION fill) on the same bars, and compares
the resulting trade set with the vectorised fast simulator.  If the two agree
(trade count, PF, P&L) the fast harness is trustworthy for grid work.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.research import custom_strategy_runtime as csr  # noqa: E402
from explore_prod import load_bars, sigs_sweep_disp, simulate_prod  # noqa: E402

conn = sqlite3.connect("judas_crew.db")
code = conn.execute("SELECT code FROM custom_strategies WHERE id=439").fetchone()[0]

symbol, tf, days = "MGC", "15m", 120
bars = load_bars(symbol, tf, days)
print(f"bars {symbol} {tf} {days}d: {len(bars)}  {bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")

params = {"lookback": 12, "min_sweep_atr": 0.3, "disp_k": 1.0, "rr": 1.5,
          "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
          "atr_period": 14}

t0 = time.time()
real = csr.run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                       symbol=symbol, qty=1, timeout_s=900,
                                       fill_mode="production")
print(f"REAL  ({time.time()-t0:.1f}s): pf={real['pf']} n={real['n_trades']} "
      f"pnl={real['total_pnl']} er={real['expectancy_r']} dd={real['max_drawdown']} "
      f"wins={real['n_wins']} losses={real['n_losses']} err={real.get('error')} "
      f"grade={real.get('promotion_grade')}")

sigs = sigs_sweep_disp(bars, dict(params, _tf=tf))
fast = simulate_prod(bars, sigs, symbol)
print(f"FAST          : pf={fast['pf']} n={fast['n_trades']} pnl={fast['total_pnl']} "
      f"er={fast['expectancy_r']} dd={fast['max_drawdown']} wins={fast['n_wins']} "
      f"losses={fast['n_losses']}  (avg risk ${fast['avg_risk_dollars']}, "
      f"cost={fast['cost_in_r']}R)")
