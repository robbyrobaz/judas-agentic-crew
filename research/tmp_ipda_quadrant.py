"""IPDA 20-bar quadrant sweep fade.

Source: YT:XYXmlyFY4Qg (Nexus Blast Trading, "ICT Liquidity Sweep Trading
Explained (IPDA Algo)").

Rules extracted from the transcript:
  1. ICT's IPDA (interbank price delivery algorithm) uses a 20-period
     look-back. The IPDA "gets triggered at midnight" (the midnight open) and
     from there it "starts looking for the last 20 bars" and "will take
     action on the sweeps, sweeping down, sweeping up".
  2. The author ports the 20-period hypothesis to 15m by "applying it in
     quadrants": the 20 bars of 15m = 5 hours, applied off fixed session
     anchors (the 19:00 -> 00:00 Asia block ends at midnight; the next
     quadrants roll the same 20-bar window forward).
  3. The action taken on a sweep is a REVERSAL back into the range.

Implementation (15m native):
  - The quadrant grid is anchored to UTC 5-hour blocks (00:00, 05:00, 10:00,
    15:00, 20:00). Each block holds exactly 20 fifteen-minute bars.
  - The reference range is the running high/low of the CURRENT quadrant
    (excluding the decision bar) -- i.e. the IPDA's "last 20 bars" window
    for the quadrant in progress.
  - A SWEEP = the decision bar trades through the quadrant range extreme and
    closes back INSIDE it (wick beyond + close back = liquidity taken).
  - We FADE the sweep: stop beyond the swept extreme, target the quadrant
    range midpoint (the "algorithms will want to go to the opposing side /
    the 50% of the range" rule).

No look-ahead: entry is the CLOSE of the decision bar, ts is the decision bar
ts, so the engine's retro-fill diagnostic stays clean (backtest_fill_valid).
"""
import numpy as np
import pandas as pd

# ---- baked parameters (the official run passes params={}, so these ARE the
# ---- intended config; the harness may override them for exploration) ----
MIN_Q_BARS = 8          # bars into the quadrant before the range is trusted
STOP_BUF_TICKS = 4      # stop buffer beyond the swept extreme
MIN_SWEEP_TICKS = 2     # minimum penetration of the range extreme
TARGET_MODE = "mid"     # "mid" = quadrant range midpoint, "r" = fixed R
TARGET_R = 1.5
ONLY_FIRST = True       # one sweep per side per quadrant
TICK = 0.10


def evaluate(bars, params):
    min_q = int(params.get("min_q_bars", MIN_Q_BARS))
    buf = float(params.get("stop_buf_ticks", STOP_BUF_TICKS)) * float(params.get("tick", TICK))
    min_sweep = float(params.get("min_sweep_ticks", MIN_SWEEP_TICKS)) * float(params.get("tick", TICK))
    target_mode = str(params.get("target_mode", TARGET_MODE))
    target_r = float(params.get("target_r", TARGET_R))
    only_first = bool(params.get("only_first", ONLY_FIRST))

    n = len(bars)
    if n < 60:
        return None

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    ts_v = bars["ts"].values

    # ---- quadrant grid: 5-hour UTC blocks = 20 fifteen-minute bars ----
    tsi = pd.DatetimeIndex(pd.to_datetime(ts_v))
    mins = tsi.hour.values * 60 + tsi.minute.values
    q = mins // 300                      # 0..4
    change = np.empty(n, dtype=bool)
    change[0] = True
    change[1:] = q[1:] != q[:-1]
    starts = np.where(change)[0]
    blk = np.searchsorted(starts, np.arange(n), side="right") - 1
    blk_start = starts[blk]
    idx_in_block = np.arange(n) - blk_start

    # ---- running range of the current quadrant, EXCLUDING the decision bar
    s_h = pd.Series(h).shift(1)
    s_l = pd.Series(l).shift(1)
    s_h[blk_start] = np.nan
    s_l[blk_start] = np.nan
    g = pd.Series(blk)
    ref_hi = s_h.groupby(g).cummax().values
    ref_lo = s_l.groupby(g).cummin().values

    # ---- has each side already been swept in this quadrant? ----
    up_swept = np.zeros(n, dtype=bool)
    dn_swept = np.zeros(n, dtype=bool)
    if only_first:
        for i in range(1, n):
            if idx_in_block[i] == 0:
                continue
            if not up_swept[blk_start[i]]:
                if h[i] > ref_hi[i] + min_sweep and c[i] < ref_hi[i]:
                    up_swept[i] = True
            if not dn_swept[blk_start[i]]:
                if l[i] < ref_lo[i] - min_sweep and c[i] > ref_lo[i]:
                    dn_swept[i] = True

    last = n - 1
    if last < 2:
        return None
    if idx_in_block[last] < min_q:
        return None
    if np.isnan(ref_hi[last]) or np.isnan(ref_lo[last]):
        return None

    # ---- bearish sweep of the quadrant high -> fade down ----
    if h[last] > ref_hi[last] + min_sweep and c[last] < ref_hi[last]:
        if not (only_first and up_swept[blk_start[last]]):
            entry = c[last]
            stop = max(h[last], ref_hi[last]) + buf
            risk = stop - entry
            if risk >= 0.5 * float(params.get("tick", TICK)):
                if target_mode == "mid":
                    mid = (ref_hi[last] + ref_lo[last]) / 2.0
                    target = mid
                else:
                    target = entry - risk * target_r
                if target < entry:
                    return {"ts": ts_v[last], "direction": "short",
                            "entry": entry, "stop": stop, "target": target}

    # ---- bullish sweep of the quadrant low -> fade up ----
    if l[last] < ref_lo[last] - min_sweep and c[last] > ref_lo[last]:
        if not (only_first and dn_swept[blk_start[last]]):
            entry = c[last]
            stop = min(l[last], ref_lo[last]) - buf
            risk = entry - stop
            if risk >= 0.5 * float(params.get("tick", TICK)):
                if target_mode == "mid":
                    mid = (ref_hi[last] + ref_lo[last]) / 2.0
                    target = mid
                else:
                    target = entry + risk * target_r
                if target > entry:
                    return {"ts": ts_v[last], "direction": "long",
                            "entry": entry, "stop": stop, "target": target}

    return None
