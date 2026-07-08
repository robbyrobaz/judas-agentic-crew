"""Cross-symbol sweep of the liquidity sweep + iFVG combined strategy."""
import sys
sys.path.insert(0, '/home/rob/judas-agentic-crew')

from src.research.custom_strategy_runtime import run_custom_backtest

with open('/home/rob/judas-agentic-crew/tmp_sweep_ifvg_strategy.py') as f:
    code = f.read()

params = {
    'sweep_lookback': 48,
    'fvg_lookback': 24,
    'min_gap_factor': 0.15,
    'rr': 2.0,
    'zone_buffer': 0.20,
}

symbols = ['MBT', 'MGC', 'MNQ', 'MCL', 'MET', 'DX', 'ZF', '6J']

print("=" * 80)
print("Liquidity Sweep + iFVG Combined Entry — Cross-Symbol Sweep (5m, 60d)")
print("=" * 80)
print(f"{'Symbol':<8} {'n':>5} {'W/L':>10} {'WR':>6} {'PF':>8} {'E[R]':>8} {'Total$':>10} {'MaxDD':>10}")
print("-" * 80)

results = {}
for sym in symbols:
    try:
        result = run_custom_backtest(
            code=code,
            symbol=sym,
            days=60,
            timeframe='5m',
        )
        n = result['n_signals']
        w = result['n_wins']
        l = result['n_losses']
        wr = (w / n * 100) if n > 0 else 0
        pf = result['pf']
        er = result['expectancy_r']
        tot = result['total_pnl']
        dd = result['max_drawdown']
        err = result.get('error')
        results[sym] = result
        if err:
            print(f"{sym:<8} ERROR: {err}")
        else:
            print(f"{sym:<8} {n:>5} {f'{w}W/{l}L':>10} {wr:>5.1f}% {pf:>8.3f} {er:>+8.3f} {tot:>+10.2f} {dd:>10.2f}")
    except Exception as e:
        print(f"{sym:<8} EXCEPTION: {type(e).__name__}: {e}")

print("=" * 80)