"""
RSI mean-reversion 5m v1 -- ICT-context-aware variant of the live-winning
buffet_zoo RSI(10,30/70) pattern.

Live evidence: retired buffet_zoo MNQ 1H RSI(10,30/70) made +$2340/14 trades;
MGC 1H RSI(10,30/70) made +$1511/9 trades. Those retired with no active
replacement -- gap in the active roster.

Architecture (5m variant):
  - Wilder RSI(10)
  - Long when RSI<30 (oversold), Short when RSI>70 (overbought)
  - Entry: next bar open
  - Stop: ATR(14) * stop_atr_mult
  - Target: target_r * risk
  - Time-of-day filter: optional NY session only (10:00-15:00 UTC)
  - Cooldown: 12 bars (1h) between signals to prevent over-trading
"""
import pandas as pd
import numpy as np


def _atr(bars, period=14):
    h = bars['high'].astype(float).values
    l = bars['low'].astype(float).values
    c = bars['close'].astype(float).values
    tr = np.maximum(h[1:] - l[1:],
                    np.maximum(np.abs(h[1:] - c[:-1]),
                               np.abs(l[1:] - c[:-1])))
    tr = np.concatenate([[h[0] - l[0]], tr])
    return pd.Series(tr).rolling(period).mean().values


def _rsi_wilder(closes, period=10):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    if n < period + 1:
        return None
    delta = np.diff(closes)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    out = np.full(n, np.nan)
    # First valid RSI at index `period` (after diff)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def evaluate(bars, params):
    period = int(params.get('period', 10))
    lo_thr = float(params.get('lo_thr', 30))
    hi_thr = float(params.get('hi_thr', 70))
    target_r = float(params.get('target_r', 1.5))
    stop_atr_mult = float(params.get('stop_atr_mult', 1.0))
    atr_period = int(params.get('atr_period', 14))
    ny_only = bool(params.get('ny_only', False))
    cooldown = int(params.get('cooldown_bars', 12))

    n = len(bars)
    if n < max(period + 5, atr_period + 5, 60):
        return None
    closes = bars['close'].astype(float).values
    highs = bars['high'].astype(float).values
    lows = bars['low'].astype(float).values
    opens = bars['open'].astype(float).values
    ts = pd.to_datetime(bars['ts'], utc=True, errors='coerce')
    if ts.isna().any():
        return None
    atr = _atr(bars, atr_period)
    rsi = _rsi_wilder(closes, period)
    last = n - 1
    if last < 1:
        return None
    cur_rsi = rsi[last]
    cur_atr = atr[last]
    if np.isnan(cur_rsi) or np.isnan(cur_atr) or cur_atr <= 0:
        return None
    direction = None
    if cur_rsi < lo_thr:
        direction = 'long'
    elif cur_rsi > hi_thr:
        direction = 'short'
    if direction is None:
        return None
    # Time-of-day filter (UTC)
    if ny_only:
        h_utc = ts.iloc[last].hour
        if h_utc < 13 or h_utc >= 20:
            return None
    # Cooldown: no fresh signal if last signal was within `cooldown` bars
    # We approximate by checking that the prior `cooldown` bars didn't
    # already trigger the same extreme (cheap proxy -- avoids over-firing)
    lookback = min(cooldown, last)
    prior_extreme = False
    for j in range(last - lookback, last):
        if np.isnan(rsi[j]):
            continue
        if direction == 'long' and rsi[j] < lo_thr:
            prior_extreme = True
            break
        if direction == 'short' and rsi[j] > hi_thr:
            prior_extreme = True
            break
    if prior_extreme:
        return None
    entry = opens[last]
    if direction == 'long':
        stop = entry - cur_atr * stop_atr_mult
        risk = entry - stop
        target = entry + target_r * risk
    else:
        stop = entry + cur_atr * stop_atr_mult
        risk = stop - entry
        target = entry - target_r * risk
    if risk <= 0:
        return None
    return {
        'ts': ts.iloc[last].isoformat(),
        'direction': direction,
        'entry': float(entry),
        'stop': float(stop),
        'target': float(target),
    }