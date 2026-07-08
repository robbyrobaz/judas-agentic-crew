"""Parameter robustness sweep for the liquidity sweep + iFVG strategy on MBT.

Since run_custom_backtest doesn't accept params, we generate param variants
of the strategy code by patching the defaults in the source.
"""
import sys
import re
sys.path.insert(0, '/home/rob/judas-agentic-crew')

from src.research.custom_strategy_runtime import run_custom_backtest

with open('/home/rob/judas-agentic-crew/tmp_sweep_ifvg_strategy.py') as f:
    base_code = f.read()

def patch_params(code, **new_params):
    """Replace default param values in the strategy code."""
    out = code
    for k, v in new_params.items():
        # Match patterns like: int(params.get('key', 0.20))
        # Or: float(params.get('key', 0.20))
        # And replace the default value (second arg)
        if isinstance(v, int):
            pattern = re.compile(r"(params\.get\(\s*['\"]" + k + r"['\"]\s*,\s*)[\d.]+(\s*\))")
            out = pattern.sub(lambda m: m.group(1) + str(v) + m.group(2), out)
        else:
            pattern = re.compile(r"(params\.get\(\s*['\"]" + k + r"['\"]\s*,\s*)[\d.]+(\s*\))")
            out = pattern.sub(lambda m: m.group(1) + str(v) + m.group(2), out)
    return out

print("=" * 100)
print("Liquidity Sweep + iFVG — Parameter Robustness on MBT (5m, 60d)")
print("=" * 100)
print(f"{'sw_lb':<6} {'fvg_lb':<6} {'gap':<6} {'rr':<4} {'zb':<5} {'n':>5} {'W/L':>10} {'WR':>6} {'PF':>8} {'E[R]':>8} {'Total$':>10}")
print("-" * 100)

baseline = {'sweep_lookback': 48, 'fvg_lookback': 24, 'min_gap_factor': 0.15, 'rr': 2.0, 'zone_buffer': 0.20}

variations = [
    {'sweep_lookback': 24},
    {'sweep_lookback': 36},
    {'sweep_lookback': 60},
    {'sweep_lookback': 72},
    {'fvg_lookback': 12},
    {'fvg_lookback': 36},
    {'fvg_lookback': 48},
    {'min_gap_factor': 0.10},
    {'min_gap_factor': 0.20},
    {'min_gap_factor': 0.25},
    {'rr': 1.5},
    {'rr': 2.5},
    {'rr': 3.0},
    {'zone_buffer': 0.10},
    {'zone_buffer': 0.30},
    {'zone_buffer': 0.50},
]

best_pf = 0
best_n = 0
best_params = None
gates_pass_count = 0
results_list = []

for variant in variations:
    params = {**baseline, **variant}
    code = patch_params(base_code, **params)
    try:
        result = run_custom_backtest(
            code=code, symbol='MBT', days=60, timeframe='5m',
        )
        n = result['n_signals']
        w = result['n_wins']
        l = result['n_losses']
        wr = (w / n * 100) if n > 0 else 0
        pf = result['pf']
        er = result['expectancy_r']
        tot = result['total_pnl']
        err = result.get('error')
        if err:
            print(f"VARIANT {variant} ERROR: {err}")
            continue
        line = f"{params['sweep_lookback']:<6} {params['fvg_lookback']:<6} {params['min_gap_factor']:<6} {params['rr']:<4} {params['zone_buffer']:<5} {n:>5} {f'{w}W/{l}L':>10} {wr:>5.1f}% {pf:>8.3f} {er:>+8.3f} {tot:>+10.2f}"
        print(line)
        results_list.append((params, result))
        # Track gates pass
        if pf >= 1.3 and n >= 15 and er > 0:
            gates_pass_count += 1
            if n > best_n or (n == best_n and pf > best_pf):
                best_pf = pf
                best_n = n
                best_params = params
    except Exception as e:
        print(f"VARIANT {variant} EXCEPTION: {type(e).__name__}: {e}")

print("-" * 100)
print(f"Gates-pass count: {gates_pass_count}/{len(variations)}")
if best_params:
    print(f"BEST: n={best_n}, PF={best_pf:.3f}, params={best_params}")
print("=" * 100)