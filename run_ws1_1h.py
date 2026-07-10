"""Run 1h variant of CSID 220 (task #2008 WS1 backup)."""
import sys, json
sys.path.insert(0, '.')
from src.research.custom_strategy_runtime import run_custom_backtest

with open('tmp_csid220_1h.py') as f:
    code = f.read()

print("=" * 70)
print("CSID 220 ADAPTED TO 1H (task #2008 WS1 backup)")
print("=" * 70)

results = {}
for sym, tick in [("MGC", 0.10), ("MBT", 0.50), ("MNQ", 0.25), ("MET", 0.10)]:
    for days in [180, 365]:
        print(f"\n--- {sym} (tick={tick}) 1h {days}d ---")
        try:
            r = run_custom_backtest(code=code, symbol=sym, days=days, timeframe="1h")
            r["tick"] = tick
            print(json.dumps(r, indent=2))
            results[f"{sym}_1h_{days}d"] = r
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}")

print("\n" + "=" * 70)
print("SUMMARY (1h variant)")
print("=" * 70)
for k, v in results.items():
    if "n_signals" in v:
        print(f"{k:20s} n={v.get('n_signals', 0):3d}  PF={v.get('pf', 0):6.2f}  "
              f"E[R]={v.get('expectancy_r', 0):+5.2f}  PnL=${v.get('total_pnl', 0):+8.2f}")