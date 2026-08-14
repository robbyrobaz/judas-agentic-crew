"""Judas-Continuation 5m test runner — sweep+displacement+pullback."""
import json
import sys
sys.path.insert(0, 'research')
from judas_continuation_5m_v1 import evaluate as _eval
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars
from src import bar_cache

CODE = open('research/judas_continuation_5m_v1.py').read()

PARAM_SETS = {
    'tight':  {'sweep_lookback': 5, 'atr_period': 14, 'body_ratio_min': 0.70, 'disp_atr_mult': 1.3, 'pullback_pct': 0.40, 'target_r': 1.5, 'stop_buffer_atr': 0.10},
    'med':    {'sweep_lookback': 6, 'atr_period': 14, 'body_ratio_min': 0.65, 'disp_atr_mult': 1.2, 'pullback_pct': 0.50, 'target_r': 1.5, 'stop_buffer_atr': 0.15},
    'loose':  {'sweep_lookback': 8, 'atr_period': 14, 'body_ratio_min': 0.60, 'disp_atr_mult': 1.1, 'pullback_pct': 0.50, 'target_r': 1.5, 'stop_buffer_atr': 0.20},
}

print('=== Judas-Continuation 5m Window-Extension Sweep ===\n')
results = []
for tf, days in [('5m', 180), ('5m', 252)]:
    for sym in ['MGC', 'MNQ', 'MCL', '6J', 'ZF']:
        try:
            bars = bar_cache.get_bars(sym, timeframe=tf)
        except Exception as e:
            print(f'{sym} {tf} bars fetch failed: {e}')
            continue
        if bars is None or len(bars) < 100:
            print(f'{sym} {tf} insufficient bars')
            continue
        per_day = {'5m': 288, '15m': 96, '1h': 24}.get(tf, 24)
        if len(bars) > per_day * days:
            bars = bars.tail(per_day * days).reset_index(drop=True)
        for ps_name, ps in PARAM_SETS.items():
            res = run_custom_backtest_on_bars(code=CODE, bars=bars, params=ps, timeout_s=60)
            n = res.get('n_signals', 0)
            w = res.get('n_wins', 0)
            l = res.get('n_losses', 0)
            pf = res.get('pf', 0)
            er = res.get('expectancy_r', 0)
            pnl = res.get('total_pnl', 0)
            dd = res.get('max_drawdown', 0)
            err = res.get('error')
            print(f'{sym} {tf} {days:3d}d {ps_name:5s}: n={n:3d} W/L={w}/{l} PF={pf:5.2f} E[R]={er:+.2f} PnL={pnl:+8.2f} DD={dd:6.2f} {"ERR="+err if err else ""}')
            results.append({'symbol': sym, 'tf': tf, 'days': days, 'params': ps_name,
                'n': n, 'w': w, 'l': l, 'pf': pf, 'er': er, 'pnl': pnl, 'dd': dd, 'err': err})

# Save
with open('research/_jc_5m_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print('\n=== TOP RESULTS BY PF (n>=20) ===')
top = [r for r in results if r['n'] >= 20 and r['pf'] >= 1.0]
for r in sorted(top, key=lambda x: -x['pf'])[:20]:
    print(f"{r['symbol']} {r['tf']} {r['days']:3d}d {r['params']:5s}: n={r['n']} PF={r['pf']:.2f} E[R]={r['er']:+.2f} PnL={r['pnl']:+.2f}")

print('\n=== TOP RESULTS BY E[R] (n>=10) ===')
top2 = [r for r in results if r['n'] >= 10 and r['er'] >= 0.3]
for r in sorted(top2, key=lambda x: -x['er'])[:20]:
    print(f"{r['symbol']} {r['tf']} {r['days']:3d}d {r['params']:5s}: n={r['n']} PF={r['pf']:.2f} E[R]={r['er']:+.2f} PnL={r['pnl']:+.2f}")
