"""
ICT Mitigation Block Continuation (15m) — with Local Structure Shift (LSS) confirmation.

Source: YT:SRESnt0r7DU + YT:Nkp29cVQf28 (FXNX 2026) — explicit mitigation block framework.

Architecture: same as CSID 326 (MGC 15m ICT Mitigation Block) but ported to 6J.
- pivot_len=8, lookback=60, min_break_atr=1.2 (vs 5m 1.0) — wider swing pivot for 15m
- LSS confirmation filter (Local Structure Shift on 15m)
- Entry at 50% equilibrium of block (mitigation block midpoint)
- Stop: block edge + 0.2 ATR buffer. Target: 2.0R.

Fresh 120d v1_realistic_micros stamp (15m native):
- 6J:  n=104, 66W/38L (63.5% WR), PF=3.17, E[R]=+0.53, +$4489, maxDD $315 — STRONG, PROPOSE

Slot gap: 6J 15m has no ict_mitigation_block active (only 5m CSID-style variant). 6J has
chronic commission bleed (-$718.75 lifetime); this 15m architecture has positive expectancy
on a faster timeframe with stronger LSS filter vs the 5m version.
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


def _rolling_swing_low(low, lookback):
    return low.shift(1).rolling(lookback).min()


def _rolling_swing_high(high, lookback):
    return high.shift(1).rolling(lookback).max()


def evaluate(bars, params):
    pivot_len = int(params.get("pivot_len", 8))
    lookback = int(params.get("lookback", 60))
    min_break_atr = float(params.get("min_break_atr", 1.2))
    require_ll_after = bool(params.get("require_ll_after", True))
    require_hh_after = bool(params.get("require_hh_after", True))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    use_equilibrium_entry = bool(params.get("use_equilibrium_entry", True))
    atr_period = int(params.get("atr_period", 14))
    use_lss = bool(params.get("use_lss", True))
    lss_lookback = int(params.get("lss_lookback", 12))
    lss_min_break = float(params.get("lss_min_break", 0.0))

    n = len(bars)
    min_bars = max(lookback + pivot_len + 5, 60)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    atr = _atr(bars, period=atr_period)
    swing_low = _rolling_swing_low(low, pivot_len)
    swing_high = _rolling_swing_high(high, pivot_len)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    direction = None
    entry = None
    stop = None

    def _lss_confirmed(dir_, idx):
        if not use_lss:
            return True
        end = idx
        start = max(0, end - lss_lookback)
        for k in range(end - 1, start - 1, -1):
            if k < 1:
                break
            if dir_ == "short":
                if pd.isna(swing_low.iloc[k]) or pd.isna(atr.iloc[k]) or atr.iloc[k] <= 0:
                    continue
                if float(close.iloc[k]) <= float(swing_low.iloc[k]) - lss_min_break * float(atr.iloc[k]):
                    return True
            else:
                if pd.isna(swing_high.iloc[k]) or pd.isna(atr.iloc[k]) or atr.iloc[k] <= 0:
                    continue
                if float(close.iloc[k]) >= float(swing_high.iloc[k]) + lss_min_break * float(atr.iloc[k]):
                    return True
        return False

    for j in range(i - 3, max(0, i - lookback) - 1, -1):
        if pd.isna(swing_low.iloc[j]) or pd.isna(atr.iloc[j]) or atr.iloc[j] <= 0:
            continue
        ref_sl = float(swing_low.iloc[j])
        cj = float(close.iloc[j])
        if cj <= ref_sl - min_break_atr * float(atr.iloc[j]):
            if j - 1 < 0:
                continue
            bl = float(low.iloc[j - 1])
            bh = float(high.iloc[j - 1])
            ll_made = True
            if require_ll_after:
                ll_made = any(float(low.iloc[k]) < cj for k in range(j + 1, i + 1))
            if not ll_made:
                continue
            if float(high.iloc[i]) >= bl and float(close.iloc[i]) <= bh * 1.001:
                if not _lss_confirmed("short", i):
                    continue
                direction = "short"
                mid = 0.5 * (bl + bh)
                entry = mid if use_equilibrium_entry else float(close.iloc[i])
                stop = bh + stop_buf_atr * cur_atr
                break

    if direction is None:
        for j in range(i - 3, max(0, i - lookback) - 1, -1):
            if pd.isna(swing_high.iloc[j]) or pd.isna(atr.iloc[j]) or atr.iloc[j] <= 0:
                continue
            ref_sh = float(swing_high.iloc[j])
            cj = float(close.iloc[j])
            if cj >= ref_sh + min_break_atr * float(atr.iloc[j]):
                if j - 1 < 0:
                    continue
                bl = float(low.iloc[j - 1])
                bh = float(high.iloc[j - 1])
                hh_made = True
                if require_hh_after:
                    hh_made = any(float(high.iloc[k]) > cj for k in range(j + 1, i + 1))
                if not hh_made:
                    continue
                if float(low.iloc[i]) <= bh * 1.001 and float(close.iloc[i]) >= bl:
                    if not _lss_confirmed("long", i):
                        continue
                    direction = "long"
                    mid = 0.5 * (bl + bh)
                    entry = mid if use_equilibrium_entry else float(close.iloc[i])
                    stop = bl - stop_buf_atr * cur_atr
                    break

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}