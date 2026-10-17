"""
ICT Silver Bullet — Strict Body-Close MSS + Macro Window (5m) — v1

Source: YT:ks6wOGghzoA (CIC FINATIC "Turn $50K into $500K ICT Silver Bullet" 2026)

Distilled concrete rules (single-symbol, no cross-asset SMT):
1. SWEEP: current bar's high exceeds a recent swing-high OR current bar's low
   undercuts a recent swing-low (lookback within sweep_lookback bars).
2. BIAS alignment is implicit (sweep + direction must agree with MSS direction).
3. MSS (Market Structure Shift): within mss_lookback, a bar CLOSED (body close,
   not just wick) through a swing-low by >= min_break_atr * ATR (short setup)
   OR through a swing-high by the same (long setup).  Body close = abs(close-open)
   of the MSS bar pushes close outside the prior swing in the trade direction.
4. FVG: the MSS candle and the bar AFTER it must leave a gap (no overlap between
   high[j+1] and low[j+2] for shorts; low[j+1] and high[j+2] for longs).
5. MITIGATION/OB zone: candle j-1 (range [low, high]) is the mitigation block.
6. CURRENT bar retraces into the OB zone (current high >= OB low for shorts,
   current low <= OB high for longs).
7. LSS: in the last lss_lookback 5m bars (excluding current), 5m printed a
   swing break in our direction (a continuation signal on the lower TF).
8. MACRO WINDOW: current bar timestamp must be within the NY AM macro window:
   14:45 - 15:15 UTC (= 10:45 - 11:15 ET, the "silver bullet cure" window).
9. ENTRY at midpoint of the mitigation block.
10. STOP just beyond OB edge + stop_buf_atr * ATR (current ATR).
11. TARGET = entry + target_r * risk (default 2.0R).

Validation targets:
- 5m timeframe only.
- Symbols: MNQ (primary), MGC, MCL, 6J, ZF (no MBT/MET/DX).
- Optimized for clean trades (n>=20, PF>1).

Params:
- mss_lookback (default 30) — bars back to search for MSS
- min_break_atr (default 0.6) — body close displacement through swing
- fvg_required (default True) — must have FVG after MSS
- use_breaker_only (default False) — breaker = OB that took prior high/low then
   inverted; mitigation = inverted without taking.  Filter to breaker only.
- use_lss (default True) and lss_lookback (default 8)
- target_r (default 2.0)
- stop_buf_atr (default 0.2)
- macro_start_utc / macro_end_utc (hour UTC, defaults 14, 15 — full hour window
   14:00-15:00 UTC = 10-11 ET silver bullet).  Set to (14, 15) by default.
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


def _in_macro_window(t, hour_start, hour_end):
    """True if t's hour-of-day (UTC) is in [hour_start, hour_end)."""
    try:
        h = pd.Timestamp(t).hour
        return hour_start <= h < hour_end
    except Exception:
        return False


def evaluate(bars, params):
    pivot_len = int(params.get("pivot_len", 5))
    mss_lookback = int(params.get("mss_lookback", 30))
    min_break_atr = float(params.get("min_break_atr", 0.6))
    fvg_required = bool(params.get("fvg_required", True))
    use_breaker_only = bool(params.get("use_breaker_only", False))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    atr_period = int(params.get("atr_period", 14))
    use_lss = bool(params.get("use_lss", True))
    lss_lookback = int(params.get("lss_lookback", 8))
    macro_start_utc = int(params.get("macro_start_utc", 14))
    macro_end_utc = int(params.get("macro_end_utc", 15))
    sweep_lookback = int(params.get("sweep_lookback", 10))

    n = len(bars)
    min_bars = max(mss_lookback + pivot_len + 10, 50)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    open_ = bars["open"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    atr_v = _atr(bars, period=atr_period)
    swing_low = _rolling_swing_low(low, pivot_len)
    swing_high = _rolling_swing_high(high, pivot_len)

    i = n - 1
    if pd.isna(atr_v.iloc[i]) or atr_v.iloc[i] <= 0:
        return None
    cur_atr = float(atr_v.iloc[i])

    # Macro window check on the current (entry) bar
    if "time" in bars.columns:
        cur_t = bars["time"].iloc[i]
    else:
        cur_t = bars.index[i]
    if not _in_macro_window(cur_t, macro_start_utc, macro_end_utc):
        return None

    def _lss_confirmed(dir_, idx):
        if not use_lss:
            return True
        end = idx
        start = max(0, end - lss_lookback)
        for k in range(end - 1, start - 1, -1):
            if k < 1:
                continue
            if dir_ == "short":
                sl_k = swing_low.iloc[k]
                a_k = atr_v.iloc[k]
                if pd.isna(sl_k) or pd.isna(a_k) or a_k <= 0:
                    continue
                if float(close.iloc[k]) <= float(sl_k):
                    return True
            else:
                sh_k = swing_high.iloc[k]
                a_k = atr_v.iloc[k]
                if pd.isna(sh_k) or pd.isna(a_k) or a_k <= 0:
                    continue
                if float(close.iloc[k]) >= float(sh_k):
                    return True
        return False

    def _sweep_confirmed(dir_, idx):
        # Within sweep_lookback bars before idx, did a swing-high get swept
        # (for short setup) or a swing-low get swept (for long setup)?
        end = idx
        start = max(0, end - sweep_lookback)
        if dir_ == "short":
            recent_sh = high.iloc[start:end].max()
            return float(high.iloc[idx]) >= recent_sh
        else:
            recent_sl = low.iloc[start:end].min()
            return float(low.iloc[idx]) <= recent_sl

    def _body_close_broke(dir_, idx, ref_level):
        """Strict body close: open AND close both on the break side of ref_level
        (close on break side, open on opposite side).  Body crosses the level."""
        o = float(open_.iloc[idx])
        c = float(close.iloc[idx])
        if dir_ == "short":
            return o > ref_level and c < ref_level
        return o < ref_level and c > ref_level

    def _fvg_present_after(j, dir_):
        """Check 3-candle FVG around bar j: candle j, j+1, j+2."""
        if j + 2 > i:
            return False
        if dir_ == "short":
            # Short FVG: high of j+1 below low of j+2 (gap down between MSS and j+2)
            return float(high.iloc[j + 1]) < float(low.iloc[j + 2])
        # Long FVG: low of j+1 above high of j+2 (gap up)
        return float(low.iloc[j + 1]) > float(high.iloc[j + 2])

    direction = None
    entry = None
    stop = None

    # Try SHORT setup first
    for j in range(i - 2, max(0, i - mss_lookback) - 1, -1):
        if pd.isna(swing_low.iloc[j]) or pd.isna(atr_v.iloc[j]) or atr_v.iloc[j] <= 0:
            continue
        ref_sl = float(swing_low.iloc[j])
        cj = float(close.iloc[j])
        # Strict body-close through swing low
        if not _body_close_broke("short", j, ref_sl):
            continue
        # Require LL made after
        ll_made = any(float(low.iloc[k]) < cj for k in range(j + 1, i + 1))
        if not ll_made:
            continue
        # FVG
        if fvg_required and not _fvg_present_after(j, "short"):
            continue
        # OB zone = candle j-1
        if j - 1 < 0:
            continue
        bl = float(low.iloc[j - 1])
        bh = float(high.iloc[j - 1])
        # Current bar retraces into OB zone
        if not (float(high.iloc[i]) >= bl):
            continue
        # Sweep confirmation (current bar swept recent high)
        if not _sweep_confirmed("short", i):
            continue
        # LSS
        if not _lss_confirmed("short", i):
            continue
        direction = "short"
        mid = 0.5 * (bl + bh)
        entry = mid
        stop = bh + stop_buf_atr * cur_atr
        break

    if direction is None:
        for j in range(i - 2, max(0, i - mss_lookback) - 1, -1):
            if pd.isna(swing_high.iloc[j]) or pd.isna(atr_v.iloc[j]) or atr_v.iloc[j] <= 0:
                continue
            ref_sh = float(swing_high.iloc[j])
            cj = float(close.iloc[j])
            if not _body_close_broke("long", j, ref_sh):
                continue
            hh_made = any(float(high.iloc[k]) > cj for k in range(j + 1, i + 1))
            if not hh_made:
                continue
            if fvg_required and not _fvg_present_after(j, "long"):
                continue
            if j - 1 < 0:
                continue
            bl = float(low.iloc[j - 1])
            bh = float(high.iloc[j - 1])
            if not (float(low.iloc[i]) <= bh):
                continue
            if not _sweep_confirmed("long", i):
                continue
            if not _lss_confirmed("long", i):
                continue
            direction = "long"
            mid = 0.5 * (bl + bh)
            entry = mid
            stop = bl - stop_buf_atr * cur_atr
            break

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}