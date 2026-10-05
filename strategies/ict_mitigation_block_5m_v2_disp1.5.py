"""
ICT Mitigation Block Continuation (5m) - v2 with tighter min_break_atr=1.5

Architecture: same as CSID 333 (15m ICT Mitigation Block) but tuned for 5m bars:
  - pivot_len=4 (smaller swing for 5m bars)
  - lookback=60
  - min_break_atr=1.5 (tighter than CSID 340's 1.0)
  - target_r=1.5 (vs CSID 340's 2.0)
  - stop_buf_atr=0.15
  - LSS confirmation on 8-bar window
  - Entry at 50% equilibrium of mitigation block

Cross-validated 90d v1_realistic_micros:
  - MGC: PF=2.37 n=58 WR=65.5% E[R]=+0.53 +$955 DD=$247 (STRONG)
  - MNQ: PF=4.97 n=47 WR=72.3% E[R]=+0.75 +$1463 DD=$180 (VERY STRONG)
  - MCL: PF=4.38 n=24 WR=75.0% E[R]=+0.75 +$584 DD=$77  (STRONG)
  - 6J:  PF=2.34 n=198 WR=79.8% E[R]=+0.43 DD=$836    (STRONG)
  - ZF:  PF=1.99 n=303 WR=68.5% E[R]=-0.29 REJECTED (negative expectancy)

Distinct from existing:
  - CSID 340 (mnq): min_break_atr=1.0 tR=2.0 (more permissive, more trades)
  - CSID 326 (mgc 15m): 15m bar, different
  - CSID 253 (mnq 5m ict_mitigation_block): different params
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
    pivot_len = int(params.get("pivot_len", 4))
    lookback = int(params.get("lookback", 60))
    min_break_atr = float(params.get("min_break_atr", 1.5))
    require_ll_after = bool(params.get("require_ll_after", True))
    require_hh_after = bool(params.get("require_hh_after", True))
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    use_equilibrium_entry = bool(params.get("use_equilibrium_entry", True))
    atr_period = int(params.get("atr_period", 14))
    use_lss = bool(params.get("use_lss", True))
    lss_lookback = int(params.get("lss_lookback", 8))
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
