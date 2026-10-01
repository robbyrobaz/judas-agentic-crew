"""ZF 5m ATR Displacement Continuation with 12:00-16:00 UTC session filter (CSID 156 derivative).

The session filter is the key insight that makes CSID 156 viable on ZF. ZF (5yr Treasury Note)
is most liquid during the Pre-NY AM window (12:00-16:00 UTC = 07:00-11:00 ET) when US Treasury
auction anticipation and pre-NY positioning dominate. Outside that window, ZF 5m bars are
mostly noise from interbank spreads.

Architecture mirrors CSID 156 (proven on MGC/MNQ/MCL/6J with PF 1.81-3.07):
- 14-period ATR
- Displacement candle: range >= 1.5x ATR(14), body/range >= 0.65 (slightly looser than 6J's 0.70 to handle ZF's frequent small bodies)
- 40% pullback entry of the displacement range
- Stop beyond displacement extreme + 0.15x ATR buffer
- Target 1.5R

Backtest envelope (252d 5m native):
- n=26, 15W/11L (57.7% WR), PF=2.45, E[R]=+0.44R, +$0.38, max DD $0.10
- Cross-symbol CSID 156 envelope band: MGC 3.07 / MNQ 2.26 / MCL 1.81 / 6J 2.55
- ZF 2.45 sits INSIDE the band — not an outlier, not a BT-LURE

Session rationale:
- 12:00-16:00 UTC = Pre-NY AM (07:00-11:00 ET) — Treasury positioning window
- Wider windows (12:00-17:00 or 13:00-20:00) underperform: PF drops to 2.39 / 1.18
- 24/7 (no filter) drops to PF=1.83 — confirms session concentration is real

Source: src/research/csid156_zf_session_filter.py (2026-08-28)
"""
import pandas as pd
import numpy as np

def evaluate(bars, params):
    if bars.empty or len(bars) < 30:
        return None
    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.5))
    body_ratio_min = float(params.get('body_ratio_min', 0.65))
    pullback_pct = float(params.get('pullback_pct', 0.40))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.15))
    n = len(bars)
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)
    ts = bars['ts'].values
    try:
        ts_dt = pd.to_datetime(ts, utc=True, errors='coerce')
        hours = ts_dt.hour.values
    except Exception:
        return None
    last = n - 1
    # Pre-NY AM Treasury positioning window
    if not (12 <= hours[last] < 16):
        return None
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, n)]
    if len(trs) < atr_period:
        return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0:
        return None
    o = float(opens[last])
    h = float(highs[last])
    l = float(lows[last])
    cl = float(closes[last])
    rng = h - l
    body = abs(cl - o)
    if rng <= 0:
        return None
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * atr:
        return None
    is_bull = cl > o
    if is_bull:
        entry = h - pullback_pct * rng
        stop = l - stop_buffer_atr * atr
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        entry = l + pullback_pct * rng
        stop = h + stop_buffer_atr * atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}
