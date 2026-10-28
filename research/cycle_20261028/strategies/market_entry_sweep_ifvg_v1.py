"""SWEEP -> FVG TAP -> IFVG INVERSION, MARKET ENTRY (Zen liquidity-sweep model).

Source
------
YT:pmexdsocN9c (Zen Trades, "The Easiest ICT Liquidity Sweep Strategy in 2026",
10:23).  The model has four mechanical steps:

  1. LIQUIDITY SWEEP -- price takes a pool (swing high/low, session extreme)
     and is rejected back inside it.
  2. HTF GAP TAP -- after the sweep, price trades back INTO a fair value gap
     left by the leg that produced the sweep.
  3. IFVG ENTRY -- the gap is INVERTED: price closes back through the far edge
     of the gap.  That close is the entry trigger (the video uses a 30s-5m
     trigger on top of a 15m+ context).
  4. THE FILTER THAT MATTERS -- "if there is no good liquidity, there is no
     good trade".  In the direction of the trade, the recent leg must have
     left STACKED SWING POOLS (low-resistance liquidity).  The video claims
     this filter alone removes ~80% of the losing trades.

WHY THIS FILE IS NOT A RETRO-FILL ARTEFACT
-----------------------------------------
Findings 9126 / 9133 / 9145 / 9146: every family in the book that names a
STRUCTURAL price (FVG midpoint, OB midpoint, the sweep extreme) is a
retro-fill artefact -- the house backtester fills at that named price and
the move from there to the decision close is never risked.

Here the ENTRY IS THE CLOSE OF THE DECISION BAR.  The gap is used only as a
TRIGGER (a close through it), never as a fill price.  The stop sits beyond
the sweep extreme, the target is `rr` x risk.  So:
    base fill == fair fill == decision bar close
    live fill  == next bar open (production semantics)
and base == fair == live.  A PF that survives here is a PF a market order
can earn.

PLACEHOLDER-BAR FILTER
-----------------------
The deep archive is padded with PLACEHOLDER bars (open=high=low=close at a
stale price, volume 0).  Those bars fabricate sweeps and fake gaps, so the
strategy only ever looks at a CONTIGUOUS RUN OF REAL BARS ending at the
decision bar.  Inside a real run this is a no-op (the honest screen ran on
clean segments only); on the raw archive it removes the contamination.
"""
import numpy as np


def _atr_from(H, L, C, start, cur, period):
    """Mean true range over the last `period` bars inside a real-bar run."""
    s = max(int(start), int(cur) - int(period), 1)
    if s > cur:
        return float("nan")
    hh = H[s:cur + 1]
    ll = L[s:cur + 1]
    pc = C[s - 1:cur]
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - pc), np.abs(ll - pc)))
    if len(tr) == 0:
        return float("nan")
    return float(np.mean(tr[-int(period):]))


def _pools(H, L, start, cur, bars_back, direction, price):
    """STEP 4 (Zen): count swing liquidity pools IN the direction of the trade.

    long  -> swing HIGHS above `price` (buy-side liquidity price can run into)
    short -> swing LOWS below `price` (sell-side liquidity price can run into)

    If the leg that carries the trade left no pools in front of it, price is
    heading into a void -- the video calls that the single best filter it has.
    """
    lo = max(int(start) + 1, int(cur) - int(bars_back))
    n = 0
    for t in range(lo, int(cur)):
        if direction == "long":
            if H[t] > H[t - 1] and H[t] >= H[t + 1] and H[t] > price:
                n += 1
        else:
            if L[t] < L[t - 1] and L[t] <= L[t + 1] and L[t] < price:
                n += 1
    return n


def evaluate(bars, params):
    lookback = int(params.get("lookback", 12))
    min_sweep_atr = float(params.get("min_sweep_atr", 0.30))
    confirm_bars = int(params.get("confirm_bars", 6))
    disp_k = float(params.get("disp_k", 0.8))
    body_ratio_min = float(params.get("body_ratio_min", 0.55))
    fvg_atr_min = float(params.get("fvg_atr_min", 0.15))
    fvg_max_age = int(params.get("fvg_max_age", 12))
    ifvg_edge = str(params.get("ifvg_edge", "near"))
    struct_bars = int(params.get("struct_bars", 24))
    struct_min_pools = int(params.get("struct_min_pools", 0))
    rr = float(params.get("rr", 1.5))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    atr_period = int(params.get("atr_period", 14))

    need = (lookback + confirm_bars + fvg_max_age + struct_bars
            + atr_period + 16)
    if len(bars) < need:
        return None

    w = bars.iloc[-need:]
    H = w["high"].to_numpy(float)
    L = w["low"].to_numpy(float)
    O = w["open"].to_numpy(float)
    C = w["close"].to_numpy(float)
    if "volume" in w.columns:
        V = w["volume"].to_numpy(float)
    else:
        V = np.ones(len(w), dtype=float)
    cur = len(w) - 1

    # ---- contiguous run of REAL bars ending at the decision bar -----------
    real = (H > L) & (V > 0)
    if not real[cur]:
        return None
    run_start = cur
    while run_start > 0 and real[run_start - 1]:
        run_start -= 1
    if (cur - run_start + 1) < lookback + 2:
        return None
    if (cur - run_start + 1) < atr_period + 1:
        return None

    a = _atr_from(H, L, C, run_start, cur, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None
    ms = min_sweep_atr * a
    buf = stop_buf_atr * a
    min_gap = fvg_atr_min * a

    # ---- 1. most recent liquidity sweep inside the confirm window --------
    j = -1
    direction = None
    for k in range(cur - 1, max(run_start, cur - confirm_bars - 1), -1):
        if k - lookback < run_start:
            continue
        phk = float(np.max(H[k - lookback:k]))
        plk = float(np.min(L[k - lookback:k]))
        if L[k] < plk - ms and C[k] > plk:      # sell-side pool taken, rejected
            j, direction = k, "long"
            break
        if H[k] > phk + ms and C[k] < phk:      # buy-side pool taken, rejected
            j, direction = k, "short"
            break
    if j < 0:
        return None

    plj = float(np.min(L[j - lookback:j]))      # prior low that was swept
    phj = float(np.max(H[j - lookback:j]))      # prior high that was swept

    # ---- 2. displacement / MSS back through the swept level ---------------
    # the leg after the sweep must have closed beyond the swept level with a
    # displacement bar (this is the structure shift of the sweep model).
    disp_ok = False
    for k in range(j + 1, cur + 1):
        rng = max(float(H[k] - L[k]), 1e-9)
        br = abs(float(C[k] - O[k])) / rng
        if rng < disp_k * a or br < body_ratio_min:
            continue
        if direction == "long" and float(C[k]) > plj:
            disp_ok = True
        elif direction == "short" and float(C[k]) < phj:
            disp_ok = True
    if not disp_ok:
        return None

    # ---- 3. the FVG left by the leg INTO the sweep, tapped + inverted ----
    # long  -> bearish FVG (gap created on the way DOWN into the sweep):
    #          price rallies back into it and closes back through it.
    # short -> bullish FVG (gap created on the way UP into the sweep):
    #          price pulls back into it and closes back through it.
    m_lo = max(run_start + 1, j - fvg_max_age)
    gap_lo = gap_hi = None
    for m in range(m_lo, j):
        if m - 1 < run_start or m + 1 > cur:
            continue
        if direction == "long":
            lo = float(L[m + 1])
            hi = float(H[m - 1])
            if lo >= hi or (hi - lo) < min_gap:
                continue
        else:
            lo = float(H[m - 1])
            hi = float(L[m + 1])
            if hi <= lo or (hi - lo) < min_gap:
                continue
        # the gap must have been TAPPED after the sweep
        tapped = False
        for t in range(j + 1, cur):
            if direction == "long":
                if float(H[t]) >= lo:
                    tapped = True
                    break
            else:
                if float(L[t]) <= hi:
                    tapped = True
                    break
        if not tapped:
            continue
        # the inversion close on the decision bar
        if direction == "long":
            hit = (float(C[cur]) > hi) if ifvg_edge == "far" else (float(C[cur]) > lo)
        else:
            hit = (float(C[cur]) < lo) if ifvg_edge == "far" else (float(C[cur]) < hi)
        if not hit:
            continue
        gap_lo, gap_hi = lo, hi
        break
    if gap_lo is None:
        return None

    # ---- 4. STEP 4 FILTER: stacked liquidity pools in the trade direction -
    if struct_min_pools > 0:
        if _pools(H, L, run_start, cur, struct_bars, direction,
                  float(C[cur])) < struct_min_pools:
            return None

    # ---- entry / brackets ------------------------------------------------
    entry = float(C[cur])
    if direction == "long":
        stop = float(L[j]) - buf
    else:
        stop = float(H[j]) + buf
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry, "stop": stop,
            "target": target, "ts": w["ts"].iloc[cur]}
