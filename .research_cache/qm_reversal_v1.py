"""
ICT Quasimodo Reversal — 15m / 5m custom evaluator.

Pattern (4-pivot structure with Break of Structure):
  BEARISH QMH (over-under / fakey): SH (LS = QM line) -> SL -> HH (Head) -> LL breaking SL
  BULLISH QML (mirror): SL (LS = QM line) -> SH -> LL (Head) -> HH breaking SH

Trigger (NOT immediate — wait for retest rejection):
  BEARISH: bar high tags QM line (within tolerance), CLOSE back below -> short.
  BULLISH: bar low tags QM line (within tolerance), CLOSE back above -> long.

Risk:
  STOP beyond Head high/low + small ATR buffer (the level that would prove wrong).
  TP = entry +/- R * risk.
  Expire if BOS pivot was more than `expiry_bars` ago.

No-repaint: every decision comes from fully closed bars.
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars.empty or len(bars) < 80:
        return None

    swing_lookback = int(params.get("swing_lookback", 5))
    atr_period = int(params.get("atr_period", 14))
    stop_buf_atr_frac = float(params.get("stop_buf_atr_frac", 0.10))
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    expiry_bars = int(params.get("expiry_bars", 30))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    n = len(bars)
    last = n - 1
    ts_v = bars["ts"].values

    min_idx_needed = atr_period + swing_lookback + 12
    if last < min_idx_needed:
        return None

    rngs = h[last - atr_period: last] - l[last - atr_period: last]
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None

    buf = stop_buf_atr_frac * atr

    tol = max(tick, 0.25 * atr, 4 * tick)

    k = swing_lookback
    pivots = []
    for i in range(last - k - 1, atr_period - 1, -1):
        if pivots and (i + k) >= pivots[-1][0]:
            continue
        ok = True
        for j in range(i - k, i):
            if h[j] >= h[i]:
                ok = False
                break
        if ok:
            for j in range(i + 1, i + k + 1):
                if h[j] >= h[i]:
                    ok = False
                    break
        if ok:
            pivots.append((i, "H"))
            if len(pivots) >= 4:
                break
            continue
        ok = True
        for j in range(i - k, i):
            if l[j] <= l[i]:
                ok = False
                break
        if ok:
            for j in range(i + 1, i + k + 1):
                if l[j] <= l[i]:
                    ok = False
                    break
        if ok:
            pivots.append((i, "L"))
            if len(pivots) >= 4:
                break

    if len(pivots) < 4:
        return None

    # pivots is most-recent-first; QM requires alternating types.
    p0, p1, p2, p3 = pivots[0], pivots[1], pivots[2], pivots[3]

    # Expire pattern if BOS was too long ago (no current relevance).
    if (last - p0[0]) > expiry_bars:
        return None

    # BEARISH QMH: types [L, H, L, H], LL below SL and HH above SH.
    if (p0[1] == "L" and p1[1] == "H" and p2[1] == "L" and p3[1] == "H"
            and l[p0[0]] < l[p2[0]] and h[p1[0]] > h[p3[0]]):
        qm_line = h[p3[0]]  # LS high
        head = h[p1[0]]
        if h[last] >= qm_line - tol and c[last] < qm_line:
            entry = c[last]
            stop = head + buf
            risk = stop - entry
            if risk <= 0.5 * tick:
                return None
            target = entry - target_r * risk
            return {"ts": ts_v[last], "direction": "short",
                    "entry": entry, "stop": stop, "target": target}

    # BULLISH QML: types [H, L, H, L], HH above SH and LL below SL.
    if (p0[1] == "H" and p1[1] == "L" and p2[1] == "H" and p3[1] == "L"
            and h[p0[0]] > h[p2[0]] and l[p1[0]] < l[p3[0]]):
        qm_line = l[p3[0]]  # LS low
        head = l[p1[0]]
        if l[last] <= qm_line + tol and c[last] > qm_line:
            entry = c[last]
            stop = head - buf
            risk = entry - stop
            if risk <= 0.5 * tick:
                return None
            target = entry + target_r * risk
            return {"ts": ts_v[last], "direction": "long",
                    "entry": entry, "stop": stop, "target": target}

    return None
