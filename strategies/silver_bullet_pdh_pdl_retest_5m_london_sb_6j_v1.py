"""CSID 416 silver_bullet_pdh_pdl_retest_5m_london_sb_6j_v1

6J-specific London Silver Bullet 3-5 AM EST (7-9 UTC) variant of the proven
CSID 270 silver_bullet_pdh_pdl_retest_5m_nogate architecture (PF=3.25 on ZF,
PF=2.89 on MCL, PF=2.42 already firing on 6J via #4851).

Architectural additions for 6J Forex:
- HARD time gate at 7-9 UTC (London pre-open / NY pre-open overlap). This is
  Atif Hussain's Forex-specific window (3-5 AM EST/EDT) per finding cHZWRm1yKfA.
- The existing 6J silver_bullet_pdh_pdl_retest_5m_nogate active (#4851, n=37
  PF=2.42) fires throughout the session. This variant ONLY fires within the
  London SB window, where 6J liquidity sweeps (per ICT) are highest-conviction.
- Tick=1e-06 to match live 6J micro-yen tick size used in active #4851.

Why 6J and not generic: 6J bleeds -$750 lifetime (16W/48L); the only other
6J London SB active (#4941 ict_mitigation_block_london_sb) is the ict_mitigation_block
family. This adds a SECOND, distinct London SB architecture for 6J (different
signal path: PDH/PDL break + retrace + FVG confirmation, NOT mitigation block).

Backtest: pending run_custom_backtest on 6J 5m.
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 320:
        return None

    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 1e-06))
    stop_buf = float(params.get("stop_buf_ticks", 4)) * tick
    break_thr = float(params.get("break_threshold_ticks", 2)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 12))
    session_start_utc = int(params.get("session_start_utc", 7))
    session_end_utc = int(params.get("session_end_utc", 9))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    ts_v = bars["ts"].values

    # TIME GATE: only allow within London Silver Bullet window 7-9 UTC
    try:
        cur_ts = pd.to_datetime(bars["ts"].iloc[-1], utc=True, errors="coerce")
        if pd.isna(cur_ts):
            return None
        hr = int(cur_ts.hour)
    except Exception:
        return None
    if not (session_start_utc <= hr < session_end_utc):
        return None

    n = len(bars)
    last = n - 1
    if last < 5:
        return None

    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=8).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=8).min().shift(1).values

    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None

    if h[last] >= pdh_series[last] + break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] < pdh_series[j] and l[j] >= pdl_series[j]:
                for k in range(j + 1, last):
                    if l[k] > h[k - 2]:
                        entry_idx = k + 1
                        if entry_idx < n:
                            entry = o[entry_idx]
                            stop = min(l[j], l[last]) - stop_buf
                            risk = entry - stop
                            if risk >= 0.5 * tick:
                                target = entry + risk * target_r
                                return {
                                    "ts": ts_v[entry_idx],
                                    "direction": "long",
                                    "entry": entry,
                                    "stop": stop,
                                    "target": target,
                                }
                break

    if l[last] <= pdl_series[last] - break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] > pdl_series[j] and h[j] <= pdh_series[j]:
                for k in range(j + 1, last):
                    if h[k] < l[k - 2]:
                        entry_idx = k + 1
                        if entry_idx < n:
                            entry = o[entry_idx]
                            stop = max(h[j], h[last]) + stop_buf
                            risk = stop - entry
                            if risk >= 0.5 * tick:
                                target = entry - risk * target_r
                                return {
                                    "ts": ts_v[entry_idx],
                                    "direction": "short",
                                    "entry": entry,
                                    "stop": stop,
                                    "target": target,
                                }
                break
    return None