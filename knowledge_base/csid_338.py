"""
ICT BSL/SSL Sweep Continuation + 1h EMA Bias (5m)

Architecture (FRESH — different from active bsl_run_judas):
  - BSL sweep: current 5m high breaks rolling-20-bar high (last 100 minutes)
  - SSL sweep: current 5m low breaks rolling-20-bar low
  - Displacement requirement: range >= 1.0 ATR, body_ratio >= 0.60
  - 1h EMA(60) bias filter:
      - Long entry only if 1h close > 1h EMA60
      - Short entry only if 1h close < 1h EMA60
  - Entry: midpoint of the sweep candle (50% of h-l-o range)
  - Stop: sweep extreme +/- 0.10 ATR buffer
  - Target: 1.5R

Differentiation vs existing families:
  - bsl_run_judas_eq_cluster_5m (CSID 276): requires CLUSTERED BSL/SSL (multi-touch),
    not single swing pivot; no HTF bias; 50% entry vs pullback entry
  - bsl_run_judas (other variants): same
  - 271 judas_continuation: requires swing high/low sweep, similar — but with bias filter
  - This strategy: simple BSL/SSL high/low sweep + 1h bias + 5m displacement

The bias filter should reduce counter-trend signals and improve PF.
"""
import numpy as np
import pandas as pd

def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
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
    body_ratio_min = float(params.get('body_ratio_min', 0.60))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.0))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.10))
    htf_ema_period = int(params.get('htf_ema_period', 60))

    n = len(bars)
    min_bars = max(sweep_lookback + 5, atr_period + 5, htf_ema_period + 5, 50)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    opn = bars["open"].astype(float)
    atr = _atr(bars, period=atr_period)

    # 1h EMA bias
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

    # BSL sweep: current high breaks recent swing high
    if cur_high > sh and cur_close < sh:
        if not h1_bias_bear:
            return None
        # Displacement close back inside range = bearish reversal
        # But this is BSL sweep; usually we enter short at midpoint on continuation of reversal
        # Actually BSL sweep with displacement close inside = SHORT signal
        # Conservative: only short if body is bearish (close < open) AND breaks back below swing high
        if cur_close >= cur_open:
            return None  # bullish candle despite sweep = likely continuation up
        entry = cur_low + 0.5 * rng
        stop = cur_high + stop_buffer_atr * cur_atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}

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
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}

    return None
