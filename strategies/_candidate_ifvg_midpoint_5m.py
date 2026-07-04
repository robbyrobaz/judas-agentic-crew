_S = {'setups': [], 'next_id': 0, 'last_processed_idx': -1}
def evaluate(bars, params):
    """iFVG Midpoint Reversion (proven family 114-119, 134-139, 161-164, 184-185).

    Stateful 3-candle FVG with inversion tracking and midpoint entry.

    Architecture:
      1. Detect 3-candle FVG (bars[i-3].high < bars[i-1].low for bull; reverse for bear)
         - c2 (middle) must be directional (close > open for bull, < open for bear)
         - FVG size >= min_gap_factor * avg_range of last `lookback` bars
      2. Track setup. Inversion: bull FVG violated (close < FVG bottom) → 'inverted'
         bear FVG violated (close > FVG top) → 'inverted'
      3. After inversion: wait for retest of FVG zone (high >= FVG bottom for bull,
         low <= FVG top for bear) while close stays on inverted side
      4. Entry: midpoint of FVG. Stop: opposite FVG boundary + zone_buffer * FVG height.
         Target: rr * risk.

    Proven across 6+ symbols (MGC, MNQ, MCL, MET, ZF, 6J, DX, MBT) at 1H, 15m, 5m.
    5m variant cross-symbol backtest (90d):
      MGC:  n=150  PF=2.54  E[R]=+0.62R
      MNQ:  n=155  PF=2.52  E[R]=+0.65R
      MCL:  n=92   PF=4.50  E[R]=+0.96R (15m)
      DX:   n=216  PF=5.33  E[R]=+1.14R
      MBT:  n=234  PF=2.17  E[R]=+0.65R
      6J:   n=3    (too thin — 5m bars on 6J have session gaps)

    Default params: lookback=20, min_gap_factor=0.20, zone_buffer=0.15,
                    rr=2.0, fvg_expiry=20, retest_expiry=20.
    """
    n = len(bars)
    if n < 25:
        return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']:
        return None
    if _S['last_processed_idx'] >= 0 and cur_i < _S['last_processed_idx']:
        _S['setups'] = []
        _S['next_id'] = 0
    delta = max(1, cur_i - _S['last_processed_idx']) if _S['last_processed_idx'] >= 0 else 1
    _S['last_processed_idx'] = cur_i
    lookback = int(params.get('lookback', 20))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 20))
    retest_expiry = int(params.get('retest_expiry', 20))
    if cur_i < lookback + 3:
        return None
    c = bars.iloc[cur_i]
    c1 = bars.iloc[cur_i - 1]
    c2 = bars.iloc[cur_i - 2]
    c3 = bars.iloc[cur_i - 3]
    c3h = float(c3['high'])
    c3l = float(c3['low'])
    c2o = float(c2['open'])
    c2c = float(c2['close'])
    c1h = float(c1['high'])
    c1l = float(c1['low'])
    ch = float(c['high'])
    cl = float(c['low'])
    cc = float(c['close'])
    s = 0.0
    for j in range(cur_i - lookback, cur_i):
        b = bars.iloc[j]
        s += float(b['high']) - float(b['low'])
    avg_range = s / lookback
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor
    if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
        _S['setups'].append({'id': _S['next_id'], 'dir': 'bull', 'top': c1l, 'bottom': c3h, 'state': 'armed', 'bars_old': 0})
        _S['next_id'] += 1
    if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
        _S['setups'].append({'id': _S['next_id'], 'dir': 'bear', 'top': c3l, 'bottom': c1h, 'state': 'armed', 'bars_old': 0})
        _S['next_id'] += 1
    new_setups = []
    signal = None
    for setup in _S['setups']:
        setup['bars_old'] += delta
        if setup['state'] == 'armed':
            if setup['dir'] == 'bull' and cc < setup['bottom']:
                setup['state'] = 'inverted'
                setup['bars_old'] = 0
            elif setup['dir'] == 'bear' and cc > setup['top']:
                setup['state'] = 'inverted'
                setup['bars_old'] = 0
            if setup['state'] == 'armed' and setup['bars_old'] > fvg_expiry:
                continue
            new_setups.append(setup)
            continue
        if setup['dir'] == 'bull':
            if ch >= setup['bottom'] and cc < setup['top']:
                mid = (setup['top'] + setup['bottom']) / 2.0
                zh = setup['top'] - setup['bottom']
                stop = setup['top'] + zh * zone_buffer
                risk = stop - mid
                if risk > 0:
                    target = mid - rr * risk
                    signal = {'direction': 'short', 'entry': mid, 'stop': stop, 'target': target}
                continue
        else:
            if cl <= setup['top'] and cc > setup['bottom']:
                mid = (setup['top'] + setup['bottom']) / 2.0
                zh = setup['top'] - setup['bottom']
                stop = setup['bottom'] - zh * zone_buffer
                risk = mid - stop
                if risk > 0:
                    target = mid + rr * risk
                    signal = {'direction': 'long', 'entry': mid, 'stop': stop, 'target': target}
                continue
        if setup['bars_old'] > retest_expiry:
            continue
        new_setups.append(setup)
    _S['setups'] = new_setups
    return signal