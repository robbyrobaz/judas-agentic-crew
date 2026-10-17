"""
ICT Judas Swing REVERSAL 15m (Faiz SMC-inspired, HSMiW6qCce4)

Architecture (FRESH: reversal, not continuation):
  - 15m bars only.
  - Detect a 'false sweep': current 15m bar's wick takes a recent swing H/L
    but the CLOSE is back on the inside (rejection). This is the
    Judas Swing / manipulation leg.
  - Require the displacement body to be >= disp_atr_mult * ATR with body_ratio_min
    so we trade strong rejection moves only.
  - 1h EMA bias filter (resampled from 15m):
      - Short entry only if 1h close > 1h EMA60 (sell in bullish bias)
      - Long entry only if 1h close < 1h EMA60 (buy in bearish bias)
    This biases us to fade the manipulation when the higher TF context
    confirms a real move is likely to follow.
  - Entry: midpoint of the sweep candle range
  - Stop: sweep extreme + stop_buffer_atr * ATR
  - Target: target_r * risk (1:2 default)

Differentiation vs existing families:
  - judas_continuation (CSID 271, 337): enter in the SAME direction as the
    sweep break (continuation). This strategy enters in the OPPOSITE
    direction (reversal/fade). They will produce different signal sets.
  - bsl_sweep_continuation_bias_5m (CSID 338): same reversal logic on 5m.
    The 15m version targets a different cadence (fewer, bigger setups).
"""
import numpy as np
import pandas as pd


def _atr(bars, period=14):
    h = bars['high'].astype(float)
    l = bars['low'].astype(float)
    c = bars['close'].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _resample_1h(bars):
    df = bars[['ts', 'open', 'high', 'low', 'close']].copy()
    df['ts'] = pd.to_datetime(df['ts'], utc=True, errors='coerce')
    df = df.dropna(subset=['ts'])
    df = df.set_index('ts')
    h1 = df.resample('1h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    return h1


def evaluate(bars, params):
    sweep_lookback = int(params.get('sweep_lookback', 20))
    atr_period = int(params.get('atr_period', 14))
    body_ratio_min = float(params.get('body_ratio_min', 0.55))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.0))
    target_r = float(params.get('target_r', 2.0))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.10))
    htf_ema_period = int(params.get('htf_ema_period', 60))

    n = len(bars)
    min_bars = max(sweep_lookback + 5, atr_period + 5, htf_ema_period + 5, 50)
    if n < min_bars:
        return None

    close = bars['close'].astype(float)
    high = bars['high'].astype(float)
    low = bars['low'].astype(float)
    opn = bars['open'].astype(float)
    atr = _atr(bars, period=atr_period)

    # 1h EMA bias from 15m bars
    try:
        h1 = _resample_1h(bars)
        if len(h1) < htf_ema_period + 1:
            return None
        h1_close = h1['close'].values.astype(float)
        h1_ema = pd.Series(h1_close).ewm(span=htf_ema_period, adjust=False).mean().values
        cur_ts = pd.to_datetime(bars['ts'].iloc[-1], utc=True, errors='coerce')
        if pd.isna(cur_ts):
            return None
        last_h1_idx = None
        for k in range(len(h1) - 1, -1, -1):
            if h1.index[k] <= cur_ts:
                last_h1_idx = k
                break
        if last_h1_idx is None or last_h1_idx < 1:
            return None
        h1_cur = float(h1_close[last_h1_idx])
        h1_ema_cur = float(h1_ema[last_h1_idx])
        h1_bias_bull = h1_cur > h1_ema_cur
        h1_bias_bear = h1_cur < h1_ema_cur
    except Exception:
        return None

    swing_high = high.shift(1).rolling(sweep_lookback).max()
    swing_low = low.shift(1).rolling(sweep_lookback).min()

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    sh = float(swing_high.iloc[i]) if not pd.isna(swing_high.iloc[i]) else None
    sl = float(swing_low.iloc[i]) if not pd.isna(swing_low.iloc[i]) else None
    if sh is None or sl is None or sh <= sl:
        return None

    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    cur_close = float(close.iloc[i])
    cur_open = float(opn.iloc[i])
    rng = cur_high - cur_low
    if rng <= 0:
        return None
    body = abs(cur_close - cur_open)
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    direction = None
    entry = None
    stop = None

    # BSL sweep reversal: wick above swing high, close back below
    if cur_high > sh and cur_close < sh:
        if not h1_bias_bear:
            return None
        if cur_close >= cur_open:
            return None
        entry = cur_low + 0.5 * rng
        stop = cur_high + stop_buffer_atr * cur_atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry - target_r * risk
        return {'direction': 'short', 'entry': entry, 'stop': stop, 'target': target}

    # SSL sweep reversal: wick below swing low, close back above
    if cur_low < sl and cur_close > sl:
        if not h1_bias_bull:
            return None
        if cur_close <= cur_open:
            return None
        entry = cur_high - 0.5 * rng
        stop = cur_low - stop_buffer_atr * cur_atr
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry + target_r * risk
        return {'direction': 'long', 'entry': entry, 'stop': stop, 'target': target}

    return None