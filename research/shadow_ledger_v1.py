"""SHADOW LEDGER — counterfactual P&L reconstruction.

For each rejected signal in 2026-10-12..2026-10-20:
  - Take entry/stop/target/direction/ts_utc
  - Walk subsequent bars (matching signal timeframe: 5m/15m/1h)
  - WIN if target touched before stop, LOSS otherwise
  - OPEN if neither touched by last available bar
  - Mid-bar ambiguity (both touched in same bar) → LOSS (adverse)

Outputs (printed to stdout):
  A. per-strategy table (strategy_id, family, symbol, tf, n, wins, losses, opens, $, avg_R, shadow_PF)
  B. per-symbol table
  C. per-session table (Asia / London / NY_AM / NY_PM) by symbol
  D. BT-envelope vs shadow-evidence disagreement table
  E. new-wave cross-check (#7368..#7372)
  F. post-reset cap allocation rank

This is SYNTHETIC / bar-touch-reconciled — explicitly labelled in the deliverable.
"""
import sqlite3, json, os, sys
from collections import defaultdict, OrderedDict
import pandas as pd

DB = 'judas_crew.db'
WIN_START = '2026-10-12T00:00:00Z'
WIN_END = '2026-10-21T00:00:00Z'  # exclusive
LEGAL = {'MGC','MNQ','MCL','6J','ZF'}

def infer_tf(strategy_family, params_tf=None):
    """Resolve timeframe from params_json or family-name suffix."""
    if params_tf in ('5m','15m','1h','1H'):
        return '5m' if params_tf in ('5m','1H') else params_tf.lower()
    if params_tf == '1H':
        return '1h'
    f = strategy_family.lower()
    # Most specific first
    if '15m' in f:
        return '15m'
    if '5m' in f:
        return '5m'
    if '1h' in f or '_60m' in f:
        return '1h'
    return '1h'  # fallback for ambiguous names

# Dollar per tick (per contract) — verified NinjaTrader Micro specs:
DOLLAR_PER_TICK = {
    'MGC': 1.00,
    'MNQ': 0.50,
    'MCL': 1.00,
    '6J':  1.25,    # micro yen
    'ZF':  7.8125,  # 5-yr treasury note (1/32 of a point = 0.00390625)
}

def reconcile_one(sig_row, bars_df):
    """Walk bars after signal; return ('WIN'|'LOSS'|'OPEN', R_multiple)."""
    sym, ts, direction = sig_row['symbol'], sig_row['ts_utc'], sig_row['direction']
    entry, stop, target = sig_row['entry'], sig_row['stop'], sig_row['target']
    if entry is None or stop is None or target is None:
        return None, 0.0
    if direction == 'long':
        risk = entry - stop
        reward = target - entry
        if risk <= 0 or reward <= 0:
            return None, 0.0
        target_r = reward / risk
    elif direction == 'short':
        risk = stop - entry
        reward = entry - target
        if risk <= 0 or reward <= 0:
            return None, 0.0
        target_r = reward / risk
    else:
        return None, 0.0

    # Bars strictly AFTER ts (the signal bar itself is excluded — assume the bar that
    # produced the signal already closed with entry filled at the close price).
    future = bars_df[bars_df['ts'] > pd.Timestamp(ts)]
    if future.empty:
        return 'OPEN', 0.0

    for _, bar in future.iterrows():
        hi, lo = bar['high'], bar['low']
        if direction == 'long':
            hit_target = hi >= target
            hit_stop = lo <= stop
        else:
            hit_target = lo <= target
            hit_stop = hi >= stop
        if hit_target and hit_stop:
            return 'LOSS', -1.0  # adverse
        if hit_target:
            return 'WIN', target_r
        if hit_stop:
            return 'LOSS', -1.0

    # Neither touched by window end
    last_close = future.iloc[-1]['close']
    if direction == 'long':
        unrealized_R = (last_close - entry) / risk
    else:
        unrealized_R = (entry - last_close) / risk
    return 'OPEN', unrealized_R

def session_of_ts_utc(ts_utc):
    """Return Asia / London / NY_AM / NY_PM from UTC timestamp.
    ICT convention (EDT, UTC-4, 2026-10-12..20):
      Asia:   ET 19:00-04:00  → UTC 23:00-08:00 (wraps midnight)
      London: ET 03:00-08:00  → UTC 07:00-12:00
      NY AM:  ET 08:00-12:00  → UTC 12:00-16:00
      NY PM:  ET 12:00-16:45  → UTC 16:00-20:45
    """
    t = pd.Timestamp(ts_utc)
    h = t.hour
    if h >= 23 or h < 4:
        return 'Asia'
    if 4 <= h < 7:
        return 'Asia-London'   # transition window (ET 00:00-03:00)
    if 7 <= h < 12:
        return 'London'
    if 12 <= h < 16:
        return 'NY_AM'
    if 16 <= h < 21:
        return 'NY_PM'
    return 'OffHours'

def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # Active timeframe map
    cur.execute("SELECT id, params_json, strategy_family, version FROM active_strategies WHERE state='active'")
    active_tf = {}
    active_meta = {}
    active_tick = {}
    inferred_tick = {}
    for sid, pj, fam, ver in cur.fetchall():
        try:
            p = json.loads(pj)
            tf = p.get('timeframe', None)
            tick = p.get('tick', None)
        except Exception:
            tf = None
            tick = None
        active_tf[sid] = infer_tf(fam, tf)
        active_meta[sid] = (fam, ver)
        if tick is not None:
            active_tick[sid] = tick

    # Inferred tick from symbol (default per-symbol tick if missing in params)
    sym_tick_default = {'MGC': 0.10, 'MNQ': 0.25, 'MCL': 0.01, '6J': 0.000001, 'ZF': 0.00390625}
    for sid in active_tf:
        # we'll look up by symbol below
        pass

    # Load signals
    cur.execute("""
        SELECT id, ts_utc, symbol, strategy_id, strategy_family, direction,
               entry, stop, target, quality_score, risk_decision
        FROM signals
        WHERE ts_utc >= ? AND ts_utc < ?
          AND entry IS NOT NULL AND stop IS NOT NULL AND target IS NOT NULL
          AND direction IN ('long','short')
          AND symbol IN ('MGC','MNQ','MCL','6J','ZF')
        ORDER BY ts_utc
    """, (WIN_START, WIN_END))
    cols = ['id','ts_utc','symbol','strategy_id','strategy_family','direction',
            'entry','stop','target','quality_score','risk_decision']
    sigs = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f'Loaded {len(sigs)} signals ({WIN_START} -> {WIN_END})', file=sys.stderr)

    # Group signals by (symbol, tf)
    sigs_by_key = defaultdict(list)
    unknown_sids = set()
    for s in sigs:
        sid = s['strategy_id']
        if sid in active_tf:
            tf = active_tf[sid]
        else:
            tf = infer_tf(s['strategy_family'])
            unknown_sids.add(sid)
        s['tf'] = tf
        sigs_by_key[(s['symbol'], tf)].append(s)

    print(f'Unknown strategy_ids (no active row): {len(unknown_sids)}', file=sys.stderr)

    # Load bars once per (symbol,tf)
    bars_cache = {}
    for sym in LEGAL:
        for tf in ('5m','15m','1h'):
            p = f'cache_1h/{sym}_{tf}.parquet'
            if not os.path.exists(p):
                print(f'MISSING bars: {p}', file=sys.stderr)
                continue
            df = pd.read_parquet(p)
            df['ts'] = pd.to_datetime(df['ts'], utc=True)
            bars_cache[(sym, tf)] = df

    # Reconcile each signal
    results = []
    for s in sigs:
        key = (s['symbol'], s['tf'])
        bars = bars_cache.get(key)
        if bars is None:
            s['outcome'] = 'NO_BARS'
            s['R'] = 0.0
        else:
            outcome, R = reconcile_one(s, bars)
            s['outcome'] = outcome
            s['R'] = R
        s['session'] = session_of_ts_utc(s['ts_utc'])
        results.append(s)

    # === (A) per-strategy table ===
    by_strat = defaultdict(lambda: {'n':0,'wins':0,'losses':0,'opens':0,'R_sum':0.0,'dollars_won':0.0,'dollars_lost':0.0})
    for s in results:
        if s['outcome'] is None:
            continue
        sid = s['strategy_id']
        fam = s['strategy_family']
        sym = s['symbol']
        key = (sid, fam, sym, s['tf'])
        d = by_strat[key]
        d['n'] += 1
        d['R_sum'] += s['R']
        if s['outcome'] == 'WIN':
            d['wins'] += 1
            d['dollars_won'] += s['R']  # in R-multiple units
        elif s['outcome'] == 'LOSS':
            d['losses'] += 1
            d['dollars_lost'] += -s['R']  # s['R'] is -1.0 → +1 unit
        else:
            d['opens'] += 1
        # track last seen
        d['last_ts'] = max(d.get('last_ts',''), s['ts_utc'])

    # Compute per-signal dollar-R for aggregation
    for s in results:
        sym = s['symbol']
        dpt = DOLLAR_PER_TICK.get(sym, 1.0)
        # risk in ticks
        if s['direction'] == 'long':
            risk_ticks = (s['entry'] - s['stop']) / 0.000001 if sym == '6J' else (s['entry'] - s['stop']) / max(s.get('_tick', 0.01), 1e-9)
        elif s['direction'] == 'short':
            risk_ticks = (s['stop'] - s['entry']) / 0.000001 if sym == '6J' else (s['stop'] - s['entry']) / max(s.get('_tick', 0.01), 1e-9)
        else:
            risk_ticks = 0
        # Get tick from active params
        sid = s['strategy_id']
        tick = active_tick.get(sid)
        if tick is None:
            tick = inferred_tick.get(s.get('strategy_family', ''), 0.01)
        t = tick if tick else sym_tick_default.get(sym, 0.01)
        try:
            t = float(t)
        except Exception:
            t = sym_tick_default.get(sym, 0.01)
        s['_tick'] = t
        if s['direction'] == 'long':
            risk_ticks = (s['entry'] - s['stop']) / s['_tick'] if s['_tick'] else 0
        elif s['direction'] == 'short':
            risk_ticks = (s['stop'] - s['entry']) / s['_tick'] if s['_tick'] else 0
        else:
            risk_ticks = 0
        dollar_risk = risk_ticks * dpt
        s['_dollar_R'] = s['R'] * dollar_risk

    # Build per-strategy rank table — R-multiple AND dollar
    rows = []
    for (sid, fam, sym, tf), d in by_strat.items():
        n = d['n']
        if n == 0:
            continue
        wr = d['wins'] / n
        gross_win_R = d['dollars_won']
        gross_loss_R = d['dollars_lost']
        if gross_loss_R > 0:
            pf = gross_win_R / gross_loss_R
        else:
            pf = float('inf') if gross_win_R > 0 else 0.0
        avg_R = d['R_sum'] / n
        net_R = d['dollars_won'] - d['dollars_lost']
        # Dollar aggregate (use mean $ risk across the strategy's signals)
        s_sigs = [s for s in results if s.get('strategy_id') == sid and s.get('tf') == tf and s.get('symbol') == sym]
        dollar_R_sum = sum(s.get('_dollar_R', 0) for s in s_sigs)
        avg_dollar_R = dollar_R_sum / n if n else 0
        rows.append({
            'strategy_id': sid,
            'strategy_family': fam,
            'symbol': sym,
            'tf': tf,
            'n': n,
            'wins': d['wins'],
            'losses': d['losses'],
            'opens': d['opens'],
            'winrate': round(wr, 4),
            'shadow_PF': round(pf, 3) if pf != float('inf') else 'INF',
            'shadow_net_R': round(net_R, 2),
            'avg_R': round(avg_R, 3),
            'shadow_$_net': round(dollar_R_sum, 2),
            'avg_$_per_trade': round(avg_dollar_R, 2),
            'last_ts': d['last_ts']
        })

    rows.sort(key=lambda r: (-r['n'], r['strategy_id']))
    print('=== (A) PER-STRATEGY SHADOW LEDGER ===')
    print(f"{'sid':>6} {'symbol':<5} {'tf':<4} {'fam':<40} {'n':>4} {'W':>4} {'L':>4} {'O':>4} {'WR':>6} {'sPF':>7} {'netR':>7} {'avgR':>6} {'sh$':>9} {'$/tr':>7}")
    for r in rows:
        print(f"{r['strategy_id']:>6} {r['symbol']:<5} {r['tf']:<4} {r['strategy_family'][:40]:<40} {r['n']:>4} {r['wins']:>4} {r['losses']:>4} {r['opens']:>4} {r['winrate']:>6.1%} {str(r['shadow_PF']):>7} {r['shadow_net_R']:>7.1f} {r['avg_R']:>6.2f} {r['shadow_$_net']:>9.2f} {r['avg_$_per_trade']:>7.2f}")

    # === (B) per-symbol table ===
    by_sym = defaultdict(lambda: {'n':0,'wins':0,'losses':0,'opens':0,'R_sum':0.0,'net_R':0.0})
    for s in results:
        if s['outcome'] is None: continue
        d = by_sym[s['symbol']]
        d['n'] += 1
        d['R_sum'] += s['R']
        if s['outcome'] == 'WIN':
            d['wins'] += 1
            d['net_R'] += s['R']
        elif s['outcome'] == 'LOSS':
            d['losses'] += 1
            d['net_R'] += s['R']  # R is negative for LOSS
        else:
            d['opens'] += 1

    print('\n=== (B) PER-SYMBOL ===')
    print(f"{'sym':<5} {'n':>5} {'W':>5} {'L':>5} {'O':>5} {'WR':>6} {'netR':>8} {'avgR':>7}")
    for sym, d in sorted(by_sym.items()):
        n = d['n']
        if n == 0: continue
        wr = d['wins'] / n
        print(f"{sym:<5} {n:>5} {d['wins']:>5} {d['losses']:>5} {d['opens']:>5} {wr:>6.1%} {d['net_R']:>8.1f} {d['R_sum']/n:>7.3f}")

    # === (C) per-session × symbol ===
    by_sess = defaultdict(lambda: defaultdict(lambda: {'n':0,'wins':0,'losses':0,'opens':0,'net_R':0.0}))
    for s in results:
        if s['outcome'] is None: continue
        d = by_sess[s['session']][s['symbol']]
        d['n'] += 1
        if s['outcome'] == 'WIN': d['wins'] += 1; d['net_R'] += s['R']
        elif s['outcome'] == 'LOSS': d['losses'] += 1; d['net_R'] += s['R']
        else: d['opens'] += 1

    print('\n=== (C) PER-SESSION × SYMBOL (net_R) ===')
    sess_order = ['Asia','Asia-London','London','NY_AM','NY_PM','OffHours']
    syms_sorted = sorted(by_sym.keys())
    print(f"{'session':<10} " + ' '.join(f'{s:>8}' for s in syms_sorted) + '   total')
    for sess in sess_order:
        if sess not in by_sess: continue
        line = f"{sess:<10} "
        total = 0
        for sym in syms_sorted:
            d = by_sess[sess].get(sym, {'net_R':0.0,'n':0})
            line += f"{d['net_R']:>8.1f}"
            total += d['net_R']
        line += f"   {total:>8.1f}"
        print(line)

    # === (D) BT-envelope disagreement ===
    # Compare active_strategies.metrics_json profit_factor to shadow_PF
    cur.execute("""
        SELECT id, symbol, strategy_family, params_json, metrics_json
        FROM active_strategies WHERE state='active'
    """)
    bt_pf = {}
    for sid, sym, fam, pj, mj in cur.fetchall():
        try:
            m = json.loads(mj) if mj else {}
        except Exception:
            m = {}
        bt_pf[sid] = m.get('profit_factor', m.get('pf', None))

    rows_d = []
    shadow_by_sid = {r['strategy_id']: r for r in rows}
    for sid, bt in bt_pf.items():
        if sid not in shadow_by_sid: continue
        r = shadow_by_sid[sid]
        if r['n'] < 10: continue  # need minimum evidence
        sp = r['shadow_PF']
        if sp == 'INF':
            sp_num = 999.0
        else:
            sp_num = float(sp)
        if bt is None:
            continue
        try:
            bt_num = float(bt)
        except Exception:
            continue
        # Disagreement: BT PF > 2 but shadow PF < 0.8 (live-decay signal)
        # OR shadow PF > 2 but BT PF < 0.8 (regime shift)
        if bt_num >= 2.0 and sp_num < 0.8:
            rows_d.append(('LIVE_DECAY', r['strategy_id'], r['symbol'], r['tf'], r['strategy_family'], bt, sp_num, r['n'], r['wins'], r['losses']))
        elif sp_num >= 2.0 and bt_num < 0.8:
            rows_d.append(('REGIME_SHIFT', r['strategy_id'], r['symbol'], r['tf'], r['strategy_family'], bt, sp_num, r['n'], r['wins'], r['losses']))
        elif bt_num >= 1.5 and sp_num < 1.0:
            rows_d.append(('WEAK_DECAY', r['strategy_id'], r['symbol'], r['tf'], r['strategy_family'], bt, sp_num, r['n'], r['wins'], r['losses']))

    print('\n=== (D) BT vs SHADOW DISAGREEMENTS (active strategies with n>=10) ===')
    if not rows_d:
        print('(none)')
    for kind, sid, sym, tf, fam, bt, sp, n, w, l in rows_d:
        print(f"  [{kind:>12}] sid={sid} {sym} {tf} {fam[:40]:<40} BT_PF={bt:>5.2f} shadow_PF={sp:>5.2f} n={n} (W={w}/L={l})")

    # === (E) new wave cross-check ===
    # The task references #7368 MGC 15m, #7369 MGC 5m, #7370 MNQ 15m, #7371 MNQ 5m, #7372 MCL 15m
    # These are the "hourly-bias + FVG midpoint pullback" family.
    # They might not exist as ACTIVE strategies yet — find them in candidates.
    cur.execute("""
        SELECT id, symbol, strategy_family, params_json, metrics_json
        FROM strategy_candidates WHERE id IN (7368,7369,7370,7371,7372)
    """)
    new_wave = []
    for cid, sym, fam, pj, mj in cur.fetchall():
        try:
            p = json.loads(pj)
            csid = p.get('custom_strategy_id', None)
            tf = p.get('timeframe', '?')
        except Exception:
            csid, tf = None, '?'
        new_wave.append((cid, sym, fam, csid, tf))
    print('\n=== (E) NEW WAVE CROSS-CHECK ===')
    for cid, sym, fam, csid, tf in new_wave:
        # Look for signals with the same csid or family substring
        if csid is not None:
            # search active_strategies for that csid
            cur.execute("SELECT id, strategy_family FROM active_strategies WHERE state='active' AND params_json LIKE ?", (f'%{csid}%',))
            sib = cur.fetchall()
        else:
            sib = []
        n_wave_sigs = sum(1 for s in results if s['strategy_family'] == fam)
        print(f"  candidate #{cid} {sym} {tf} csid={csid} fam={fam[:40]}")
        print(f"    sibling strategies in active_strategies: {len(sib)}")
        print(f"    signals in window matching family: {n_wave_sigs}")

    # === (F) POST-RESET CAP ALLOCATION ===
    # Rank by shadow_PF and net_R, but require n >= 20
    eligible = [r for r in rows if r['n'] >= 20]
    eligible.sort(key=lambda r: (-float(r['shadow_PF']) if r['shadow_PF'] != 'INF' else -999, -r['shadow_net_R']))

    print('\n=== (F) POST-RESET CAP ALLOCATION RANK (n>=20) ===')
    print(f"{'rank':>4} {'sid':>6} {'symbol':<5} {'tf':<4} {'fam':<46} {'n':>4} {'sPF':>7} {'netR':>8} {'tier':<10}")
    for i, r in enumerate(eligible[:30], 1):
        sp = float(r['shadow_PF']) if r['shadow_PF'] != 'INF' else 999.0
        if sp >= 1.3: tier = 'T1'
        elif sp >= 1.0: tier = 'T2'
        else: tier = 'T3'
        print(f"{i:>4} {r['strategy_id']:>6} {r['symbol']:<5} {r['tf']:<4} {r['strategy_family'][:46]:<46} {r['n']:>4} {str(r['shadow_PF']):>7} {r['shadow_net_R']:>8.1f} {tier:<10}")

    # Dump full per-strategy results for the deliverable
    out_path = 'outputs/research/shadow_ledger_per_strategy.csv'
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pd.DataFrame(rows).to_csv(out_path, index=False)
    print(f'\n[full per-strategy results -> {out_path}]', file=sys.stderr)

    # Also dump per-signal rows for further analysis
    sig_out_path = 'outputs/research/shadow_ledger_per_signal.csv'
    pd.DataFrame([{k:v for k,v in s.items() if k != 'agent_notes'} for s in results]).to_csv(sig_out_path, index=False)
    print(f'[per-signal results -> {sig_out_path}]', file=sys.stderr)

    con.close()

if __name__ == '__main__':
    main()