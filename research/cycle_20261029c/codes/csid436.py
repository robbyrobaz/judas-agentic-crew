"""
NY AM Judas Swing + iFVG Midpoint Reversion (5m).

Concept: At the New York open (13:30-15:00 UTC), price typically makes a Judas
swing — false move in one direction to grab Asian/London liquidity before
reversing. Inside the Judas swing, a 3-candle iFVG forms (momentum → inversion).
This strategy enters at the iFVG midpoint after inversion, targeting via 2R.

Entry rules:
  - Session gate: 13:30-15:00 UTC (NY AM)
  - iFVG detected in last 20 bars (3-candle: bar[i-2].h < bar[i].l for bullish
    inverted FVG, or bar[i-2].l > bar[i].h for bearish)
  - Min gap = avg_range * 0.20
  - After iFVG formation, closes[k] must violate the FVG zone (inversion)
  - Current bar must retest the iFVG zone from opposite side
  - Entry at midpoint of (now-inverted) iFVG zone
  - Stop at far edge + 0.15 * zone_height buffer
  - Target: 2R from entry
  - Risk filter: risk > 0.5*tick, risk <= 3.5*ATR

Distinct from existing families:
- bsl_run_judas (csid 276, 280): uses BSL levels, not iFVG inversion
- ifvg_midpoint_reversion (csid 318, 366): no Judas swing window gate
- silver_bullet_pdh_pdl: targets PDH/PDL, not via 2R
- inside_bar_disp_breakout: mother-bar pattern, not 3-candle iFVG
- judas_continuation (csid 277): trades WITH Judas, not against

v1_realistic_micros 90d 5m native stamps:
  MGC: n=217, 120W/97L, PF=2.05, E[R]=+0.48R, +$3,134.75
"""
import pandas as pd
import numpy as np


def _parse_ts_utc(bars):
    if 'ts' not in bars.columns:
        return None
    try:
        return pd.to_datetime(bars['ts'].iloc[-1], utc=True, errors='coerce')
    except Exception:
        return None


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None
    n = len(bars)
    cur_i = n - 1

    cur_ts = _parse_ts_utc(bars)
    if cur_ts is None or pd.isna(cur_ts):
        return None
    h_utc = int(cur_ts.hour)
    m_utc = int(cur_ts.minute)
    h_dec = h_utc + m_utc / 60.0
    if not (13.5 <= h_dec < 15.0):
        return None

    lookback = int(params.get('lookback', 24))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    rr = float(params.get('rr', 2.0))
    zone_buffer = float(params.get('zone_buffer', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 20))
    atr_period = int(params.get('atr_period', 14))
    max_risk_atr = float(params.get('max_risk_atr', 3.5))

    if n < lookback + fvg_expiry + 4:
        return None

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)

    ch = highs[cur_i]; cl = lows[cur_i]; cc = closes[cur_i]

    trs = []
    for j in range(max(1, cur_i - atr_period + 1), cur_i + 1):
        tr = max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
        trs.append(tr)
    if len(trs) < atr_period // 2:
        return None
    atr = float(sum(trs)) / max(len(trs), 1)
    if atr <= 0:
        return None

    s = 0.0; cnt = 0
    for j in range(max(0, cur_i - lookback), cur_i):
        s += highs[j] - lows[j]
        cnt += 1
    if cnt == 0:
        return None
    avg_range = s / cnt
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    for i in range(cur_i - 3, max(cur_i - fvg_expiry - 3, 2), -1):
        c3h = highs[i-2]; c3l = lows[i-2]
        c1h = highs[i];   c1l = lows[i]
        c2c = closes[i-1]; c2o = opens[i-1]

        if c3h < c1l and (c1l - c3h) >= min_gap and c2c > c2o:
            top = c1l; bot = c3h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] < bot:
                    inverted = True
                    break
            if not inverted:
                continue
            if ch >= bot and cc < top:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = top + zh * zone_buffer
                risk = stop - mid
                if risk <= 0 or risk > max_risk_atr * atr:
                    continue
                target = mid - rr * risk
                return {'direction': 'short', 'entry': mid, 'stop': stop, 'target': target}

        if c3l > c1h and (c3l - c1h) >= min_gap and c2c < c2o:
            top = c3l; bot = c1h
            inverted = False
            for k in range(i, cur_i):
                if closes[k] > top:
                    inverted = True
                    break
            if not inverted:
                continue
            if cl <= top and cc > bot:
                mid = (top + bot) / 2.0
                zh = top - bot
                stop = bot - zh * zone_buffer
                risk = mid - stop
                if risk <= 0 or risk > max_risk_atr * atr:
                    continue
                target = mid + rr * risk
                return {'direction': 'long', 'entry': mid, 'stop': stop, 'target': target}

    return None
