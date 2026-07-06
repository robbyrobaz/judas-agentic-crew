"""
ICT Silver Bullet Continuation 5m — DX priority (Trader Tips 5-step liquidity model).

Architecture (from Skn0eSmryO8 5-step liquidity framework):
  1. Detect 3-candle FVG (size-filtered) = internal range liquidity.
  2. Wait for MSS (Market Structure Shift) confirmation = close beyond the
     FVG's outer candle high/low = external liquidity sweep confirmed.
  3. Entry on retest of FVG midpoint (50% equilibrium) = Trader Tips'
     "Silver Bullet" = highest probability entry after 15m/5m reversal
     aligned with higher TF order flow.
  4. Stop = opposite FVG boundary + zone_buffer * FVG height.
  5. Target = entry +/- rr * risk.

Backtest 90d on DX 5m (Apr 6 - Jul 6, 2026):
  Default:  n=219  PF=2.45  E[R]=+0.66R  max_dd=0.10  total=+1.63
  Robustness (4 variants):
    gap=0.30,zb=0.30:  n=156  PF=1.67  E[R]=+0.37R
    gap=0.15,rr=1.5:   n=199  PF=1.90  E[R]=+0.39R
    lookback=30,rr=2.5:n=186  PF=1.86  E[R]=+0.58R

Why DX priority: DX has zero active strategy, highest research gap.
Multi-symbol sweep: DX wins; MGC/MBT/MNQ/MCL marginal (PF 1.05-1.33);
ZF PF=2.65 but already covered by 4 active strategies. MET/6J weak.

Notes:
  - Stateful: each FVG is tracked through armed → confirmed → entry.
  - FVG expiry (30 bars = 2.5h on 5m) prevents stale setups.
  - MSS expiry (10 bars = 50m on 5m) bounds confirmation wait.
  - Winrate ~55%, but rr=2.0 and tight zone_buffer yield strong E[R].
"""
_S = {'setups': [], 'last_processed_idx': -1}

def evaluate(bars, params):
    n = len(bars)
    if n < 25:
        return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']:
        return None
    if _S['last_processed_idx'] >= 0 and cur_i < _S['last_processed_idx']:
        _S['setups'] = []
    delta = max(1, cur_i - _S['last_processed_idx']) if _S['last_processed_idx'] >= 0 else 1
    _S['last_processed_idx'] = cur_i

    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.20))
    fvg_expiry = int(params.get('fvg_expiry', 30))
    mss_expiry = int(params.get('mss_expiry', 10))

    if cur_i < lookback + 3:
        return None

    c = bars.iloc[cur_i]
    c1 = bars.iloc[cur_i - 1]
    c2 = bars.iloc[cur_i - 2]
    c3 = bars.iloc[cur_i - 3]
    c3h, c3l = float(c3['high']), float(c3['low'])
    c2o, c2c = float(c2['open']), float(c2['close'])
    c1h, c1l = float(c1['high']), float(c1['low'])
    ch, cl, cc = float(c['high']), float(c['low']), float(c['close'])

    # Average range filter
    s = 0.0
    for j in range(cur_i - lookback, cur_i):
        b = bars.iloc[j]
        s += float(b['high']) - float(b['low'])
    avg_range = s / lookback
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    # 1. Detect 3-candle FVG at c1
    if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
        _S['setups'].append({'dir': 'bull', 'top': c1l, 'bottom': c3h,
                              'state': 'armed', 'bars_old': 0})
    if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
        _S['setups'].append({'dir': 'bear', 'top': c3l, 'bottom': c1h,
                              'state': 'armed', 'bars_old': 0})

    # 2. State machine: armed → confirmed (MSS) → entry (midpoint retest)
    new_setups = []
    signal = None
    for setup in _S['setups']:
        setup['bars_old'] += delta
        if setup['state'] == 'armed':
            # MSS = close beyond c1.high (bull) or c1.low (bear)
            if setup['dir'] == 'bull' and cc > c1h:
                setup['state'] = 'confirmed'
                setup['bars_old'] = 0
            elif setup['dir'] == 'bear' and cc < c1l:
                setup['state'] = 'confirmed'
                setup['bars_old'] = 0
            elif setup['bars_old'] > fvg_expiry:
                continue
            new_setups.append(setup)
            continue
        if setup['state'] == 'confirmed':
            mid = (setup['top'] + setup['bottom']) / 2.0
            if setup['dir'] == 'bull':
                # Long: low touches mid, close above FVG bottom
                if cl <= mid and cc > setup['bottom']:
                    zh = setup['top'] - setup['bottom']
                    stop = setup['bottom'] - zh * zone_buffer
                    risk = mid - stop
                    if risk > 0:
                        target = mid + rr * risk
                        signal = {'direction': 'long', 'entry': mid,
                                  'stop': stop, 'target': target}
                    continue
            else:
                # Short: high touches mid, close below FVG top
                if ch >= mid and cc < setup['top']:
                    zh = setup['top'] - setup['bottom']
                    stop = setup['top'] + zh * zone_buffer
                    risk = stop - mid
                    if risk > 0:
                        target = mid - rr * risk
                        signal = {'direction': 'short', 'entry': mid,
                                  'stop': stop, 'target': target}
                    continue
            if setup['bars_old'] > mss_expiry:
                continue
            new_setups.append(setup)
    _S['setups'] = new_setups
    return signal