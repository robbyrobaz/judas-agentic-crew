"""Vectorized signal generators for local exploration (Family A + Family B)."""
from __future__ import annotations

import numpy as np
import pandas as pd

BIG = 1e9


# --------------------------------------------------------------------------
# Family A: IPDA 20-bar quadrant sweep fade (YT:XYXmlyFY4Qg)
# --------------------------------------------------------------------------
def quadrant_blocks(ts: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    tsi = pd.DatetimeIndex(pd.to_datetime(ts.values))
    mins = tsi.hour.values * 60 + tsi.minute.values
    q = mins // 300                      # 5-hour UTC quadrants
    n = len(q)
    change = np.empty(n, dtype=bool)
    change[0] = True
    change[1:] = q[1:] != q[:-1]
    starts = np.where(change)[0]
    blk = np.searchsorted(starts, np.arange(n), side="right") - 1
    blk_start = starts[blk]
    return blk, blk_start, np.arange(n) - blk_start


def _block_extreme(vals: np.ndarray, blk: np.ndarray, blk_start: np.ndarray,
                   is_max: bool) -> np.ndarray:
    """Running max/min of the CURRENT block, EXCLUDING the current bar."""
    sh = np.roll(vals, 1).astype(float)
    sh[0] = np.nan
    sh[blk_start] = np.nan
    sh = np.nan_to_num(sh, nan=(-np.inf if is_max else np.inf),
                        posinf=np.inf, neginf=-np.inf)
    w = sh + BIG * blk
    cmmax = np.maximum.accumulate(w)
    cmmin = np.minimum.accumulate(w)
    return (cmmax - BIG * blk) if is_max else (cmmin - BIG * blk)


def ipda_quadrant_sigs(bars, min_q=8, buf_ticks=4, min_sweep_ticks=2,
                       target_mode="mid", target_r=1.5, only_first=True, tick=0.10):
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    n = len(bars)
    blk, blk_start, idx_in_block = quadrant_blocks(bars["ts"])
    ref_hi = _block_extreme(h, blk, blk_start, True)
    ref_lo = _block_extreme(l, blk, blk_start, False)
    buf = buf_ticks * tick
    ms = min_sweep_ticks * tick

    bear = (h > ref_hi + ms) & (c < ref_hi) & (idx_in_block >= min_q)
    bull = (l < ref_lo - ms) & (c > ref_lo) & (idx_in_block >= min_q)

    if only_first:
        df = pd.DataFrame({"blk": blk, "idx": np.arange(n)})
        df["b"] = bear
        df["u"] = bull
        fb = set(df[df.b].groupby("blk")["idx"].min().tolist())
        fu = set(df[df.u].groupby("blk")["idx"].min().tolist())
        bear = np.array([i in fb for i in range(n)])
        bull = np.array([i in fu for i in range(n)])

    sigs = {}
    for i in range(n):
        if bear[i]:
            entry = c[i]
            stop = max(h[i], ref_hi[i]) + buf
            risk = stop - entry
            if risk < 0.5 * tick:
                continue
            target = (ref_hi[i] + ref_lo[i]) / 2.0 if target_mode == "mid" else entry - risk * target_r
            if target < entry:
                sigs[i] = ("short", entry, stop, target)
        elif bull[i]:
            entry = c[i]
            stop = min(l[i], ref_lo[i]) - buf
            risk = entry - stop
            if risk < 0.5 * tick:
                continue
            target = (ref_hi[i] + ref_lo[i]) / 2.0 if target_mode == "mid" else entry + risk * target_r
            if target > entry:
                sigs[i] = ("long", entry, stop, target)
    return sigs


# --------------------------------------------------------------------------
# Family B: ICT Vault model (YT:OlmtDCBdAkQ)
#   HTF bias -> sweep AGAINST the bias (buy-stops raid in a bearish tape)
#   -> break of market structure -> FVG (imbalance) -> entry at the FVG
#   -> target the range midpoint (premium -> discount).
# --------------------------------------------------------------------------
def ict_vault_sigs(bars, bias_mode="day_prev", sweep_lb=8, mss_window=6,
                   fvg_after=1, require_fvg=True, target_mode="day_mid",
                   target_r=2.0, buf_ticks=4, tick=0.10, min_q=0):
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts = pd.to_datetime(bars["ts"].values)
    buf = buf_ticks * tick

    # ---- daily aggregates (UTC date) ----
    day = ts.floor("D")
    day_key = (ts - pd.Timestamp("1970-01-01", tz="UTC")).dt.days.values
    day_open = pd.Series(o).groupby(day_key).transform("first").values.astype(float)
    day_close = c.copy()
    day_high = pd.Series(h).groupby(day_key).transform("max").values.astype(float)
    day_low = pd.Series(l).groupby(day_key).transform("min").values.astype(float)
    day_mid = (day_high + day_low) / 2.0

    # ---- bias ----
    bias = np.zeros(n)
    if bias_mode == "day_prev":
        df = pd.DataFrame({"d": day_key, "o": o, "c": c})
        prev = df.groupby("d")[["o", "c"]].first()
        prev["dir"] = np.sign(prev["c"] - prev["o"])
        dirs = prev["dir"].to_dict()
        keys = list(prev.index)
        cur = 0
        dmap = {}
        for k in keys:
            dmap[k] = cur
            cur = dirs[k]
        for i in range(n):
            k = day_key[i]
            bias[i] = dmap.get(k, 0)
    elif bias_mode == "ema":
        ema = pd.Series(c).ewm(span=20, adjust=False).mean().values
        bias = np.sign(c - ema)
    elif bias_mode == "day":
        bias = np.sign(day_close - day_open)

    # ---- sweep detection: extreme of the last `sweep_lb` bars, closed back
    prev_hi = pd.Series(h).rolling(sweep_lb, min_periods=sweep_lb).max().shift(1).values
    prev_lo = pd.Series(l).rolling(sweep_lb, min_periods=sweep_lb).min().shift(1).values
    up_sweep = (h > prev_hi) & (c < prev_hi)          # buy-stops taken
    dn_sweep = (l < prev_lo) & (c > prev_lo)          # sell-stops taken

    sigs = {}
    for i in range(2, n - 1):
        b = bias[i]
        if b == 0:
            continue
        if b < 0 and up_sweep[i]:
            direction = "short"
            swept_extreme = h[i]
            structure = l[i]
        elif b > 0 and dn_sweep[i]:
            direction = "long"
            swept_extreme = l[i]
            structure = h[i]
        else:
            continue
        # ---- market structure break after the sweep ----
        mss_idx = None
        for j in range(i + 1, min(n - 1, i + mss_window + 1)):
            if direction == "short" and c[j] < structure:
                mss_idx = j
                break
            if direction == "long" and c[j] > structure:
                mss_idx = j
                break
        if mss_idx is None:
            continue
        entry_idx = mss_idx
        if require_fvg:
            # bearish FVG: h[k-2] > l[k]; bullish FVG: l[k-2] < h[k]
            fvg_idx = None
            for k in range(mss_idx, min(n - 1, mss_idx + fvg_after + 1)):
                if direction == "short" and h[k - 2] > l[k]:
                    fvg_idx = k
                    break
                if direction == "long" and l[k - 2] < h[k]:
                    fvg_idx = k
                    break
            if fvg_idx is None:
                continue
            entry_idx = fvg_idx
        entry = c[entry_idx]
        if direction == "short":
            stop = max(swept_extreme, prev_hi[i]) + buf
            risk = stop - entry
            if risk < 0.5 * tick:
                continue
            target = day_mid[entry_idx] if target_mode == "day_mid" else entry - risk * target_r
            if target >= entry:
                continue
        else:
            stop = min(swept_extreme, prev_lo[i]) - buf
            risk = entry - stop
            if risk < 0.5 * tick:
                continue
            target = day_mid[entry_idx] if target_mode == "day_mid" else entry + risk * target_r
            if target <= entry:
                continue
        sigs[entry_idx] = (direction, entry, stop, target)
    return sigs
