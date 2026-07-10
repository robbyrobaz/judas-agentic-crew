"""WS1 walk-forward + cross-symbol + param robustness for 1h variant."""
import sys, json
sys.path.insert(0, '.')
from src.research.custom_strategy_runtime import run_custom_backtest

with open('tmp_csid220_1h.py') as f:
    code = f.read()

print("=" * 70)
print("WS1 ROBUSTNESS: walk-forward on MGC 1h + cross-symbol 4 remaining")
print("=" * 70)

# Cross-symbol sweep on remaining 4 symbols
for sym, tick in [("DX", 0.005), ("6J", 0.000001), ("MCL", 0.01), ("ZF", 0.0078125)]:
    for days in [180]:
        try:
            r = run_custom_backtest(code=code, symbol=sym, days=days, timeframe="1h")
            r["tick"] = tick
            print(f"{sym} 1h {days}d: n={r.get('n_signals',0)}, PF={r.get('pf',0):.2f}, "
                  f"E[R]={r.get('expectancy_r',0):+.2f}, PnL=${r.get('total_pnl',0):+.2f}")
        except Exception as e:
            print(f"{sym} 1h {days}d: ERROR {e}")

print("\n" + "=" * 70)
print("PARAM ROBUSTNESS on MGC 1h (CSID 220 1h variant)")
print("=" * 70)
# Test param variants on MGC 1h
variants = [
    {"target_r": 1.5, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 36},
    {"target_r": 2.5, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 8, "break_threshold_ticks": 4, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 6, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 48},
    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 24},
    {"target_r": 3.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 4, "break_threshold_ticks": 4, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 2, "max_retest_bars": 36},
    {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 12},
]
base = {"target_r": 2.0, "stop_buf_ticks": 6, "break_threshold_ticks": 4, "max_retest_bars": 36}
for i, v in enumerate(variants):
    p = dict(base)
    p.update(v)
    p["tick"] = 0.10
    try:
        # patch params into code via wrapper
        with open('tmp_csid220_1h.py') as f:
            base_code = f.read()
        # inject params via override
        r = run_custom_backtest(code=base_code, symbol="MGC", days=180, timeframe="1h")
        # params are read by evaluate() so the defaults are used — variant test must pass params through
        # We'll patch via a wrapper
        wrapper = base_code.replace(
            "def evaluate(bars, params):",
            "def evaluate(bars, params):\n    params = {**params, **%r}" % p
        )
        r = run_custom_backtest(code=wrapper, symbol="MGC", days=180, timeframe="1h")
        print(f"v{i+1} {v}: n={r.get('n_signals',0)}, PF={r.get('pf',0):.2f}, "
              f"E[R]={r.get('expectancy_r',0):+.2f}")
    except Exception as e:
        print(f"v{i+1}: ERROR {e}")