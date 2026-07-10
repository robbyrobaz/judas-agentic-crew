"""Run CSID 220 extended-window backtest via direct Python call (task #2008 WS1)."""
import sys
import json
sys.path.insert(0, '.')
from src.research.custom_strategy_runtime import run_custom_backtest, run_custom_backtest_on_bars
from src.research.custom_strategy_runtime import _fetch_synthetic_or_live_bars

# Load CSID 220 code from disk
with open('tmp_csid220.py') as f:
    code = f.read()

print("=" * 70)
print("CSID 220 MGC EXTENDED WINDOW TESTS (task #2008 WS1)")
print("=" * 70)

results = {}
for sym, tick in [("MGC", 0.10), ("MBT", 0.50), ("MNQ", 0.25), ("MET", 0.10)]:
    for days in [240, 365]:
        print(f"\n--- {sym} (tick={tick}) 5m {days}d ---")
        try:
            r = run_custom_backtest(code=code, symbol=sym, days=days, timeframe="5m")
            # patch tick
            r["tick"] = tick
            print(json.dumps(r, indent=2))
            results[f"{sym}_{days}d"] = r
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")
            results[f"{sym}_{days}d"] = {"error": str(e)}

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
for k, v in results.items():
    if "n_signals" in v:
        print(f"{k:20s} n={v.get('n_signals', 0):3d}  PF={v.get('pf', 0):6.2f}  "
              f"E[R]={v.get('expectancy_r', 0):+5.2f}  PnL=${v.get('total_pnl', 0):+8.2f}")
    else:
        print(f"{k:20s} ERROR: {v.get('error', '?')[:50]}")