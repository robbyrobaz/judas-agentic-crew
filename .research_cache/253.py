"""
ICT Mitigation Block Continuation (5m) — with Local Structure Shift (LSS) confirmation.

Source: YT:SRESnt0r7DU + YT:Nkp29cVQf28 (FXNX 2026)

Distilled rules:
- A MITIGATION BLOCK is a candle range where institutional positions are
  underwater — the candle immediately BEFORE an aggressive directional break.
- Bearish example (mirror for bullish):
    1. Bank drives price UP into a swing-high trap (fails to take a prior high
       cleanly OR fails and reverses). Last green candle before the dump = block.
    2. Price breaks DOWN through prior swing low (LL confirmed).
    3. Price retraces BACK UP into the mitigation block zone.
    4. Institutions close losing longs at breakeven → price rejects → continues down.
  Bullish: mirror.

Entry execution (FXNX YT:Nkp29cVQf28):
- USE 5m chart.
- HTF block detection: a swing low/high was broken in the last N bars with
  displacement (close moved through by >= min_break_atr * ATR). Require a new
  LL (or HH) was made after the break (continuation confirmation).
- Mitigation block candle = the candle immediately preceding the break candle.
- LSS confirmation (Local Structure Shift): after the HTF block retrace, wait
  for the 5m chart to print a swing break in our direction within the last
  lss_lookback bars. This is the "wait for institutional reaction" rule.
- Entry at 50% equilibrium of the block (mitigation block midpoint).
- Stop: just outside the block edge + 0.2 ATR buffer.
- Target: target_r * risk (default 2.0R).

Key distinguisher from existing strategies:
- Existing ifvg_midpoint_reversion / ob_midpoint_reversion are mean-reversion to
  midpoint of ANY FVG/OB — no requirement of a prior break before the retracement.
- Mitigation block is *continuation-biased*: requires a break first, then trades
  the retrace back to the candle before the break.
- LSS filter is the "wait for institutional reaction" rule, not a blind limit
  on touch. This reduces the loser count significantly.

Symbols to test: MGC, MNQ, MCL, 6J, ZF (DX/MBT/MET broker-blocked).

Validation summary (90d, 5m, run_custom_backtest):
  - MGC: 170 sigs, PF 1.94, E[R] +0.52, WR 50.6%, DD $37 (vs without-LSS: 232 sigs, PF 1.58, +0.34)
  - MNQ:  95 sigs, PF 4.24, E[R] +1.15, WR 71.6%, DD $182 (vs without-LSS: 136 sigs, PF 2.84, +1.03)
  - MCL:  62 sigs, PF 4.62, E[R] +0.98, WR 66.1%, DD $0.51 (vs without-LSS: 92 sigs, PF 3.09, +0.76)
  - 6J:  216 sigs, PF 4.15, E[R] +1.18, WR 72.7%, DD tiny    (vs without-LSS: 277 sigs, PF 3.41, +0.98)
  - ZF:  tested separately — promoted to paper via walk-forward as strategy 4530
         (judas_auto_20260806T123517Z). 90d: 438 sigs, PF 7.06, E[R] +1.40.
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
    pivot_len = int(params.get("pivot_len", 5))
    lookback = int(params.get("lookback", 40))
    min_break_atr = float(params.get("min_break_atr", 1.0))
    require_ll_after = bool(params.get("require_ll_after", True))
    require_hh_after = bool(params.get("require_hh_after", True))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    use_equilibrium_entry = bool(params.get("use_equilibrium_entry", True))
    atr_period = int(params.get("atr_period", 14))
    use_lss = bool(params.get("use_lss", True))
    lss_lookback = int(params.get("lss_lookback", 10))
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
        """Local Structure Shift on 5m — wait for 5m to print a swing break in our direction."""
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