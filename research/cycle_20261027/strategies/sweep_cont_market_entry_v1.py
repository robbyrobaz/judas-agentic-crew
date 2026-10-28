"""SWEEP -> DISPLACEMENT CONTINUATION, MARKET ENTRY (15m / 5m).

Architecture (honest-fill cycle 2026-10-27)
--------------------------------------------
  1. LIQUIDITY SWEEP -- within the last `confirm_bars` bars, price takes out
     a rolling extreme (max/min of the last `lookback` bars) by at least
     `min_sweep_atr` x ATR and CLOSES BACK INSIDE that range (rejection,
     not acceptance).
  2. DISPLACEMENT -- the decision bar closes back through the swept level
     with a body >= `body_ratio_min` of its own range and a range >=
     `disp_k` x ATR ("no displacement, no trade").
  3. ENTRY -- MARKET at the CLOSE of the decision bar.  There is NO structural
     entry price anywhere in this file.  Findings 9126 / 9133 / 9145 / 9146
     showed that every family in the book which names a structural price
     (FVG midpoint, OB midpoint, sweep extreme) is a RETRO-FILL artefact:
     the house backtester fills at that named price, the move from there to
     the decision close is never risked, and the edge evaporates under the
     production fill (market at the next bar's open).
  4. STOP -- beyond the sweep extreme (+ `stop_buf_atr` x ATR).
  5. TARGET -- `rr` x risk.

PLACEHOLDER-BAR FILTER
-----------------------
The deep archive is padded with PLACEHOLDER bars (open=high=low=close at a
stale price, volume 0) wherever IBKR history was missing -- 7,769 of 22,263
MGC 15m bars.  Those bars would create fake sweeps and fake displacement
candles, so the strategy only ever looks at a CONTIGUOUS RUN OF REAL BARS
(range > 0 and volume > 0) ending at the decision bar.  Inside a real run
this is a no-op (the honest screen ran on clean segments only), but it makes
the strategy safe on the raw archive and on any feed that carries gaps.

Honest evidence (LIVE book = fill at the NEXT bar's open, brackets
re-anchored, cost_model=v1_realistic_micros, qty=1, MGC 15m, one clean
segment 2026-07-21 -> 2026-10-26, IS/OOS split by TIME 60/40):

    IS  (first 60%)  n=39  PF=1.768  +$3,226.79
    OOS (last  40%)  n=32  PF=1.645  +$1,642.90
    FULL             n=71  PF=1.722  +$4,869.69   maxDD $2,029.03
    base == fair == live  (retro-fill premium 0.000 -> the house stamp is
    the same number a market order can earn)

Neighbourhood plateau (same screen, same segment): 8 of 12 sweep_cont cells
survive IS>1 AND OOS>1 with full PF 1.09-1.72 -- a family, not a lone spike.
"""
import numpy as np


def _atr_from(h, l, c, start, cur, period):
    """Mean of the last `period` true ranges, computed inside a real-bar run.

    `start` is the first index of the run (the run's first bar has no valid
    predecessor inside the run, so its true range is excluded -- the same
    convention the clean-segment screen uses).
    """
    s = max(int(start), int(cur) - int(period), 1)
    if s > cur:
        return float("nan")
    hh = h[s:cur + 1]
    ll = l[s:cur + 1]
    pc = c[s - 1:cur]
    tr = np.maximum(hh - ll, np.maximum(np.abs(hh - pc), np.abs(ll - pc)))
    if len(tr) == 0:
        return float("nan")
    return float(np.mean(tr[-int(period):]))


def evaluate(bars, params):
    lookback = int(params.get("lookback", 8))
    min_sweep_atr = float(params.get("min_sweep_atr", 0.45))
    disp_k = float(params.get("disp_k", 1.0))
    rr = float(params.get("rr", 1.5))
    confirm_bars = int(params.get("confirm_bars", 6))
    body_ratio_min = float(params.get("body_ratio_min", 0.55))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    atr_period = int(params.get("atr_period", 14))

    need = lookback + confirm_bars + atr_period + 8
    if len(bars) < need:
        return None

    # Only the tail is needed -> O(1) work per bar, so the house engine's
    # per-bar replay stays inside its 60s wall-clock cap.
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

    # ---- 1. most recent liquidity sweep inside the confirm window --------
    j = -1
    for k in range(cur - 1, max(run_start, cur - confirm_bars - 1), -1):
        if k - lookback < run_start:
            continue
        phk = float(np.max(H[k - lookback:k]))
        plk = float(np.min(L[k - lookback:k]))
        if L[k] < plk - ms and C[k] > plk:      # swept the lows, closed back in
            j = k
            break
        if H[k] > phk + ms and C[k] < phk:      # swept the highs, closed back in
            j = k
            break
    if j < 0:
        return None

    plj = float(np.min(L[j - lookback:j]))
    phj = float(np.max(H[j - lookback:j]))
    rng = max(float(H[cur] - L[cur]), 1e-9)
    br = abs(float(C[cur] - O[cur])) / rng

    # ---- 2. displacement back through the swept level ---------------------
    if L[j] < plj - ms and C[j] > plj:
        if not (float(C[cur]) > plj and br >= body_ratio_min and rng >= disp_k * a):
            return None
        direction = "long"
        stop = float(L[j]) - buf
    else:
        if not (float(C[cur]) < phj and br >= body_ratio_min and rng >= disp_k * a):
            return None
        direction = "short"
        stop = float(H[j]) + buf

    # ---- 3. MARKET entry at the decision bar's close -----------------------
    entry = float(C[cur])
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry, "stop": stop,
            "target": target, "ts": w["ts"].iloc[cur]}
