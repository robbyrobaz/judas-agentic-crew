"""Backtest iFVG midpoint reversion htf_bias on MGC 15m with htf_period variations."""
import json
import sys
sys.path.insert(0, 'research')
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars
from src import bar_cache

# Load CSID 235 code (iFVG midpoint reversion htf_bias)
csid235_code = None
import sqlite3
conn = sqlite3.connect('judas_crew.db')
row = conn.execute("SELECT code FROM custom_strategies WHERE id = 235").fetchone()
conn.close()
csid235_code = row[0]

print('=== iFVG midpoint reversion 15m MGC: htf_period sweep ===\n')

results = []
for htf in [50, 60, 70, 80, 110, 200]:
    for tf in ['15m']:
        for days in [180, 252]:
            sym = 'MGC'
            try:
                bars = bar_cache.get_bars(sym, timeframe=tf)
            except Exception as e:
                print(f'{sym} {tf} fetch failed: {e}')
                continue
            if bars is None or len(bars) < 100:
                print(f'{sym} {tf} insufficient')
                continue
            per_day = {'5m': 288, '15m': 96, '1h': 24}.get(tf, 24)
            if len(bars) > per_day * days:
                bars = bars.tail(per_day * days).reset_index(drop=True)
            ps = {'lookback': 20, 'min_gap_factor': 0.25, 'rr': 2.0, 'zone_buffer': 0.15, 'fvg_expiry': 20, 'htf_ema_period': htf}
            res = run_custom_backtest_on_bars(code=csid235_code, bars=bars, params=ps, timeout_s=60)
            n = res.get('n_signals', 0)
            w = res.get('n_wins', 0)
            l = res.get('n_losses', 0)
            pf = res.get('pf', 0)
            er = res.get('expectancy_r', 0)
            pnl = res.get('total_pnl', 0)
            dd = res.get('max_drawdown', 0)
            err = res.get('error')
            print(f'{sym} {tf} {days:3d}d htf={htf:3d}: n={n:3d} W/L={w}/{l} PF={pf:5.2f} E[R]={er:+.2f} PnL={pnl:+8.2f} DD={dd:6.2f} {"ERR="+err if err else ""}')
            results.append({'symbol': sym, 'tf': tf, 'days': days, 'htf': htf, 'n': n, 'w': w, 'l': l, 'pf': pf, 'er': er, 'pnl': pnl, 'dd': dd})

with open('research/_ifvg_15m_results.json', 'w') as f:
    json.dump(results, f, indent=2)

print('\n=== TOP BY PF (n>=10) ===')
for r in sorted([r for r in results if r['n'] >= 10], key=lambda x: -x['pf'])[:15]:
    print(f"{r['symbol']} {r['tf']} {r['days']:3d}d htf={r['htf']:3d}: n={r['n']:3d} PF={r['pf']:.2f} E[R]={r['er']:+.2f} PnL={r['pnl']:+.2f} DD={r['dd']:.2f}")
