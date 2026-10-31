"""Debug why the official engine returned n_signals=0 for the session-gated code."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

from explore_prod import load_bars  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261031a" /
        "market_entry_sweep_cont_london_15m_v1.py").read_text()

bars = load_bars("MGC", "15m", 60)
print("bars", len(bars), bars["ts"].iloc[0], bars["ts"].iloc[-1])
print("cols", list(bars.columns))
print("ts dtype", bars["ts"].dtype)

ns = {"n": 0, "err": 0, "exc": ""}
import numpy as np  # noqa: E402

g = {}
exec(CODE, g)
evaluate = g["evaluate"]

# direct call on a slice, to surface the exception the engine swallows
for i in (200, 500, 1000, 2000):
    sl = bars.iloc[: i + 1]
    try:
        sig = evaluate(sl, {})
        print(f"i={i} ts={sl['ts'].iloc[-1]} hour={sl['ts'].iloc[-1].hour} sig={sig}")
    except Exception:
        ns["err"] += 1
        print(f"i={i} EXCEPTION:")
        traceback.print_exc()
        break

# what does the engine see?
res = csr.run_custom_backtest_on_bars(code=CODE, bars=bars, params={},
                                       symbol="MGC", qty=1, timeout_s=600,
                                       fill_mode="production")
print({k: res.get(k) for k in ("n_signals", "n_trades", "pf", "total_pnl", "error")})
