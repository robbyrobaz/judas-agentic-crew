#!/usr/bin/env python3
"""MCL/6J strategy audit — classification + architecture breakdown.

Output: structured report printed to stdout, plus a CSV `state/mcl_6j_audit.csv`.
"""
import csv, json
from collections import defaultdict
from datetime import datetime, timezone

ROOT_CUTOFF = datetime(2026, 10, 15, 18, 30, 0, tzinfo=timezone.utc)

# Load active strategies
strats = []
with open('/tmp/mcl_6j_strats.csv') as f:
    for r in csv.DictReader(f):
        r['id'] = int(r['id'])
        strats.append(r)

# Load trades, group by strategy
trades_by_sid = defaultdict(list)
with open('/tmp/mcl_6j_trades_all.csv') as f:
    for r in csv.DictReader(f):
        try:
            pnl = float(r['pnl_dollars'])
        except Exception:
            pnl = 0.0
        sid = r['strategy_id']
        if not sid:
            continue
        trades_by_sid[int(sid)].append({
            'closed_at': r['closed_at'],
            'pnl': pnl,
        })

# Load demotions per strategy
demotions_by_sid = defaultdict(int)
with open('/tmp/mcl_6j_demotions.csv') as f:
    for r in csv.DictReader(f):
        if not r['strategy_id']:
            continue
        demotions_by_sid[int(r['strategy_id'])] = int(r['n_demotions'])

def max_consec_lossers(tlist):
    if not tlist:
        return 0
    max_run = 0
    run = 0
    for t in tlist:
        if t['pnl'] <= 0:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return max_run

def pf_net(gw, gl):
    if gl == 0:
        return float('inf') if gw > 0 else 0.0
    return round(gw / -gl, 3)

# Architecture group mapper
def arch(family):
    if 'ict_mitigation_block' in family: return 'ict_mb'
    if 'silver_bullet' in family: return 'silver_bullet'
    if 'ob_midpoint' in family: return 'ob_midpoint'
    if 'ifvg' in family: return 'ifvg'
    if 'bsl_run_judas' in family: return 'bsl_run_judas'
    if 'judas' in family: return 'judas_cont'
    if 'atr_disp' in family: return 'atr_disp'
    if 'turtle' in family: return 'turtle_soup'
    if 'inside_bar' in family: return 'inside_bar'
    if 'asian_range' in family: return 'asian_range'
    if 'propulsion' in family: return 'propulsion'
    if 'unicorn' in family: return 'unicorn'
    if 'swing_reclaim' in family: return 'swing_reclaim'
    if 'sfp_' in family: return 'sfp'
    if 'htf_1h_disp' in family: return 'htf_pullback'
    return 'other'

audit_rows = []
arch_agg = defaultdict(lambda: {'n': 0, 'pnl': 0.0, 'gw': 0.0, 'gl': 0.0, 'wins': 0, 'n_trades': 0})

for s in strats:
    sid = s['id']
    symbol = s['symbol']
    family = s['strategy_family']
    tlist = trades_by_sid.get(sid, [])
    n = len(tlist)
    cum_pnl = round(sum(t['pnl'] for t in tlist), 2)
    wins = sum(1 for t in tlist if t['pnl'] > 0)
    losses = sum(1 for t in tlist if t['pnl'] < 0)
    gw = round(sum(t['pnl'] for t in tlist if t['pnl'] > 0), 2)
    gl = round(sum(t['pnl'] for t in tlist if t['pnl'] < 0), 2)
    consec = max_consec_lossers(tlist)
    pn = pf_net(gw, gl)
    last_fire = max((t['closed_at'] for t in tlist), default=None)
    days_since = None
    if last_fire:
        try:
            last_dt = datetime.fromisoformat(last_fire.replace('Z', '+00:00'))
            days_since = (ROOT_CUTOFF - last_dt).days
        except Exception:
            pass
    bt_metric = {}
    try:
        bt_metric = json.loads(s['metrics_json'])
    except Exception:
        pass
    bt_pf = bt_metric.get('pf') or bt_metric.get('profit_factor')
    bt_n = bt_metric.get('n_trades')
    bt_pnl = bt_metric.get('total_pnl')
    # classification
    verdict = 'INSUFFICIENT_DATA'
    if n >= 10:
        if n >= 20 and cum_pnl < -100 and pn < 0.85:
            verdict = 'RETIRE_CANDIDATE_HARD'
        elif n >= 10 and pn < 0.85:
            verdict = 'RETIRE_CANDIDATE_LIGHT'
        elif consec >= 6 and cum_pnl < 0:
            verdict = 'RETIRE_CANDIDATE_LIGHT'
        elif days_since is not None and days_since >= 14 and cum_pnl < 0:
            verdict = 'STALE_NEGATIVE'
        elif pn > 1.05:
            verdict = 'LIVE_WINNER'
        else:
            verdict = 'NEUTRAL_FILLED'
    elif n >= 5:
        if pn > 1.05:
            verdict = 'PROMISING'
        elif pn < 0.85:
            verdict = 'WATCH_LIGHT'
    audit_rows.append({
        'sid': sid,
        'symbol': symbol,
        'family': family,
        'arch': arch(family),
        'params_strategy_name': json.loads(s['params_json']).get('strategy_name', ''),
        'n_trades': n,
        'cum_pnl': cum_pnl,
        'wins': wins,
        'losses': losses,
        'gw': gw,
        'gl': gl,
        'pf_net': pn,
        'max_consec_l': consec,
        'last_fire': last_fire,
        'days_since_fire': days_since,
        'bt_pf': bt_pf,
        'bt_n': bt_n,
        'bt_pnl': bt_pnl,
        'n_demotions': demotions_by_sid.get(sid, 0),
        'verdict': verdict,
    })
    a = arch(family)
    arch_agg[(symbol, a)]['n'] += 1
    arch_agg[(symbol, a)]['pnl'] += cum_pnl
    arch_agg[(symbol, a)]['gw'] += gw
    arch_agg[(symbol, a)]['gl'] += gl
    arch_agg[(symbol, a)]['wins'] += wins
    arch_agg[(symbol, a)]['n_trades'] += n

# Print per-strategy report
print('='*100)
print('PER-STRATEGY AUDIT — 66 active MCL/6J strategies')
print('='*100)
for r in sorted(audit_rows, key=lambda r: (r['symbol'], r['verdict'], r['pf_net'])):
    n_pf = 'inf' if r['pf_net'] == float('inf') else f"{r['pf_net']:.2f}"
    bt_pf_disp = f"{r['bt_pf']:.2f}" if isinstance(r['bt_pf'], (int, float)) else '-'
    print(f"[{r['symbol']}] sid={r['sid']:5d} {r['family']:55s} arch={r['arch']:12s} | n={r['n_trades']:3d} W={r['wins']:2d}/L={r['losses']:2d} cum=${r['cum_pnl']:+8.2f} pf={n_pf} consL={r['max_consec_l']:2d} days_since={str(r['days_since_fire']) if r['days_since_fire'] is not None else '-':>3s} BT_PF={bt_pf_disp:>5s} n_dem={r['n_demotions']} | {r['verdict']}")

print('')
print('='*100)
print('ARCHITECTURE BREAKDOWN (per symbol)')
print('='*100)
for (sym, a), agg in sorted(arch_agg.items(), key=lambda x: (x[0][0], x[0][1])):
    pf_a = pf_net(agg['gw'], agg['gl'])
    pf_disp = 'inf' if pf_a == float('inf') else f"{pf_a:.2f}"
    wr = (agg['wins']/agg['n_trades']*100) if agg['n_trades'] > 0 else 0
    print(f"  {sym} {a:18s} n_strats={agg['n']:3d} n_trades={agg['n_trades']:4d} wins={agg['wins']:3d} cum=${agg['pnl']:+9.2f} PF={pf_disp:>6s} WR={wr:5.1f}%")

# Verdict tallies
print('')
print('='*100)
print('VERDICT TALLIES')
print('='*100)
tallies = defaultdict(int)
tallies_cum = defaultdict(float)
for r in audit_rows:
    tallies[(r['symbol'], r['verdict'])] += 1
    tallies_cum[(r['symbol'], r['verdict'])] += r['cum_pnl']
for k, v in sorted(tallies.items(), key=lambda x: (x[0][0], x[0][1])):
    print(f"  {k[0]} {k[1]:25s} n={v:3d}  cum_PnL=${tallies_cum[k]:+9.2f}")

# Hard retire list
print('')
print('='*100)
print('HARD RETIRE CANDIDATES')
print('='*100)
for r in sorted(audit_rows, key=lambda r: r['cum_pnl']):
    if r['verdict'] == 'RETIRE_CANDIDATE_HARD':
        print(f"  {r['symbol']} sid={r['sid']} ({r['family']}) | n={r['n_trades']} cum=${r['cum_pnl']:+.2f} pf={r['pf_net']} consL={r['max_consec_l']}")

print('')
print('='*100)
print('LIVE WINNERS')
print('='*100)
for r in sorted(audit_rows, key=lambda r: -r['pf_net']):
    if r['verdict'] == 'LIVE_WINNER':
        print(f"  {r['symbol']} sid={r['sid']} ({r['family']}) strategy_name={r['params_strategy_name']} | n={r['n_trades']} cum=${r['cum_pnl']:+.2f} pf={r['pf_net']} gw={r['gw']} gl={r['gl']}")

# Save CSV
with open('/tmp/mcl_6j_audit.csv', 'w', newline='') as f:
    fieldnames = list(audit_rows[0].keys())
    w = csv.DictWriter(f, fieldnames=fieldnames)
    w.writeheader()
    for r in audit_rows:
        w.writerow(r)

# Save summary
with open('/tmp/mcl_6j_summary.txt', 'w') as f:
    f.write(f"Total active MCL/6J strategies: {len(audit_rows)}\n")
    sym_totals = defaultdict(lambda: {'pnl': 0.0, 'n_trades': 0, 'n_wins': 0, 'n_strats': 0, 'gw': 0.0, 'gl': 0.0})
    for r in audit_rows:
        t = sym_totals[r['symbol']]
        t['pnl'] += r['cum_pnl']
        t['n_trades'] += r['n_trades']
        t['n_wins'] += r['wins']
        t['n_strats'] += 1
        t['gw'] += r['gw']
        t['gl'] += r['gl']
    for s in ['MCL', '6J']:
        t = sym_totals[s]
        pf = (t['gw']/-t['gl']) if t['gl'] < 0 else 0
        wr = (t['n_wins']/t['n_trades']*100) if t['n_trades'] else 0
        f.write(f"{s}: n_strats={t['n_strats']} n_trades={t['n_trades']} W={t['n_wins']}/L={t['n_trades']-t['n_wins']} cum=${t['pnl']:+.2f} PF={pf:.3f} WR={wr:.1f}%\n")
