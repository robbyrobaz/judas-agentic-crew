"""Stage-by-stage funnel counter for the sweep-MSS-FVG strategy.

Answers: which filter kills every trade?  (0 trades on MGC 15m / 90d means
one of the gates is unreachable, not that the edge is absent.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PER_DAY = {"5m": 288, "15m": 96, "1h": 24}


def atr_series(H, L, C, period=14):
    n = len(C)
    tr = np.empty(n)
    tr[0] = H[0] - L[0]
    pc = C[:-1]
    tr[1:] = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - pc), np.abs(L[1:] - pc)))
    out = np.empty(n)
    run = 0.0
    for i in range(n):
        run += tr[i]
        if i >= period:
            run -= tr[i - period]
        out[i] = run / float(min(i + 1, period))
    return out


def funnel(symbol="MGC", tf="15m", days=90, lb=20, ms=0.25, sweep_age=12,
           disp_k=0.8, body_min=0.5, fvg_min=0.10, fvg_exp=8,
           stop_buf=0.10, max_risk=2.0, liq_lb=60, min_rr=1.0):
    df = pd.read_parquet(ROOT / f"data/bars_archive/{symbol}_{tf}.parquet")
    df = df.sort_values("ts").reset_index(drop=True)
    per = PER_DAY[tf]
    df = df.tail(per * days).reset_index(drop=True)
    H = df["high"].to_numpy(float)
    L = df["low"].to_numpy(float)
    C = df["close"].to_numpy(float)
    O = df["open"].to_numpy(float)
    ATR = atr_series(H, L, C)
    n = len(df)

    c = dict(sweep=0, mss=0, fvg=0, tap=0, zone=0, risk=0, tgt=0, final=0)
    for cur in range(140, n):
        a_cur = ATR[cur]
        sweep_k = -1
        direction = None
        pool_hi = pool_lo = sweep_ext = 0.0
        for k in range(cur - 1, max(0, cur - sweep_age - 1), -1):
            if k - lb < 2:
                break
            a_k = ATR[k]
            if a_k <= 0:
                continue
            ph = float(np.max(H[k - lb:k]))
            pl = float(np.min(L[k - lb:k]))
            if L[k] < pl - ms * a_k and C[k] > pl:
                sweep_k, direction = k, "long"
                pool_hi, pool_lo, sweep_ext = ph, pl, float(L[k])
                break
            if H[k] > ph + ms * a_k and C[k] < ph:
                sweep_k, direction = k, "short"
                pool_hi, pool_lo, sweep_ext = ph, pl, float(H[k])
                break
        if sweep_k < 0:
            continue
        c["sweep"] += 1
        entry = float(C[cur])
        hit_mss = hit_fvg = hit_tap = hit_zone = False
        for m in range(cur, sweep_k, -1):
            if m - 2 <= sweep_k:
                break
            rng = float(H[m] - L[m])
            if rng <= 0:
                continue
            br = abs(float(C[m] - O[m])) / rng
            if rng < disp_k * float(ATR[m]) or br < body_min:
                continue
            if direction == "long":
                if float(C[m]) <= pool_hi:
                    continue
                hit_mss = True
                bot, top = float(H[m - 2]), float(L[m])
                if not (top > bot) or (top - bot) < fvg_min * float(ATR[m]):
                    continue
                hit_fvg = True
                tap = -1
                for j in range(m + 1, min(cur, m + fvg_exp) + 1):
                    if float(L[j]) <= top and float(H[j]) >= bot:
                        tap = j
                        break
                if tap != cur:
                    continue
                hit_tap = True
                if not (bot <= entry <= top):
                    continue
                hit_zone = True
                break
            else:
                if float(C[m]) >= pool_lo:
                    continue
                hit_mss = True
                bot, top = float(L[m - 2]), float(H[m])
                if not (top > bot) or (top - bot) < fvg_min * float(ATR[m]):
                    continue
                hit_fvg = True
                tap = -1
                for j in range(m + 1, min(cur, m + fvg_exp) + 1):
                    if float(H[j]) >= bot and float(L[j]) <= top:
                        tap = j
                        break
                if tap != cur:
                    continue
                hit_tap = True
                if not (bot <= entry <= top):
                    continue
                hit_zone = True
                break
        if not hit_mss:
            continue
        c["mss"] += 1
        if not hit_fvg:
            continue
        c["fvg"] += 1
        if not hit_tap:
            continue
        c["tap"] += 1
        if not hit_zone:
            continue
        c["zone"] += 1
        stop = sweep_ext - stop_buf * a_cur if direction == "long" else sweep_ext + stop_buf * a_cur
        risk = (entry - stop) if direction == "long" else (stop - entry)
        if risk <= 0 or risk > max_risk * a_cur:
            continue
        c["risk"] += 1
        w0 = max(0, m - liq_lb)
        if direction == "long":
            cand = H[w0:m][H[w0:m] > entry + min_rr * risk]
            if cand.size == 0:
                continue
            target = float(np.min(cand))
        else:
            cand = L[w0:m][L[w0:m] < entry - min_rr * risk]
            if cand.size == 0:
                continue
            target = float(np.max(cand))
        c["tgt"] += 1
        c["final"] += 1
    print(symbol, tf, days, "d  ", c)
    return c


if __name__ == "__main__":
    for sym, tf in (("MGC", "15m"), ("MNQ", "15m"), ("MGC", "5m")):
        funnel(sym, tf, 90)
    print("--- looser gates (MGC 15m) ---")
    funnel("MGC", "15m", 90, lb=12, ms=0.15, disp_k=0.5, body_min=0.4,
           fvg_min=0.05, fvg_exp=12, max_risk=3.0, min_rr=0.6)
