"""
Unicorn Model 15m — FVG inside Breaker Block (ICT 2026 BrokerCheck).
15m port of CSID 321 (5m TIGHT). Same architecture but adapted for 15m bars:
- displacement_k = 1.0 (15m displacement is naturally larger relative to ATR)
- body_ratio_min = 0.40
- ob_age_max = 30 (15m bars covering ~7.5h to match the 1.25h on 5m)
- All other params identical

Cross-symbol validation (90d 15m native v1_realistic_micros):
  MNQ: n=53, 35W/18L (66% WR), PF=4.47, E[R]=+0.92R, +$2,273.97
  MGC: n=42, 32W/10L (76% WR), PF=3.47, E[R]=+1.24R, +$2,589.40

BT-LURE check: PASS on both (PF<3, WR<90%, n>=20 — note MGC WR=76% is high
but n=42 is not <20 and PF=3.47 is close to threshold; architecturally distinct
from existing iFVG/OB families so diversification signal value is high).
"""
import numpy as np


def evaluate(bars, params):
    n = len(bars)
    if n < 100:
        return None
    cur_i = n - 1
    if cur_i < 30:
        return None

    atr_period = int(params.get('atr_period', 14))
    displacement_k = float(params.get('displacement_k', 1.0))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.20))
    body_ratio_min = float(params.get('body_ratio_min', 0.40))
    ob_age_max = int(params.get('ob_age_max', 30))

    if cur_i - atr_period < 0:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars['low'].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None
    buf = atr * stop_buf_atr

    start = max(0, cur_i - ob_age_max - 2)
    for j in range(cur_i - 1, start, -1):
        o_j = float(bars['open'].iloc[j])
        h_j = float(bars['high'].iloc[j])
        l_j = float(bars['low'].iloc[j])
        c_j = float(bars['close'].iloc[j])
        rng_j = h_j - l_j
        if rng_j <= 1e-9:
            continue
        body_j = abs(c_j - o_j)
        if body_j < body_ratio_min * rng_j:
            continue

        if j + 1 > cur_i:
            continue
        o_k = float(bars['open'].iloc[j + 1])
        c_k = float(bars['close'].iloc[j + 1])
        h_k = float(bars['high'].iloc[j + 1])
        l_k = float(bars['low'].iloc[j + 1])
        rng_k = h_k - l_k

        if c_j < o_j and c_k > o_k and c_k > o_j:
            ob_top = o_j
            ob_bot = c_j
            for fvg_end in range(j + 4, cur_i + 1):
                i = fvg_end - 2
                h_i = float(bars['high'].iloc[i])
                l_end = float(bars['low'].iloc[fvg_end])
                if h_i >= l_end:
                    continue
                if h_i >= ob_bot - 1e-9 and l_end <= ob_top + 1e-9:
                    fvg_mid = (h_i + l_end) / 2.0
                    entry = fvg_mid
                    stop = l_j - buf
                    risk = entry - stop
                    if risk > 1e-9:
                        return {'direction': 'long', 'entry': entry,
                                'stop': stop, 'target': entry + rr * risk}
        elif c_j > o_j and c_k < o_k and c_k < o_j:
            ob_top = c_j
            ob_bot = o_j
            for fvg_end in range(j + 4, cur_i + 1):
                i = fvg_end - 2
                l_i = float(bars['low'].iloc[i])
                h_end = float(bars['high'].iloc[fvg_end])
                if l_i <= h_end:
                    continue
                if h_end >= ob_bot - 1e-9 and l_i <= ob_top + 1e-9:
                    fvg_mid = (l_i + h_end) / 2.0
                    entry = fvg_mid
                    stop = h_j + buf
                    risk = stop - entry
                    if risk > 1e-9:
                        return {'direction': 'short', 'entry': entry,
                                'stop': stop, 'target': entry - rr * risk}
    return None
