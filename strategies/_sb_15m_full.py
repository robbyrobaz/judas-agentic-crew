import numpy as np, pandas as pd
def evaluate(bars, params):
    if bars.empty or len(bars) < 320: return None
    tr = float(params.get("target_r", 2.0))
    tk = float(params.get("tick", 0.10))
    sb = float(params.get("stop_buf_ticks", 6)) * tk
    bt = float(params.get("break_threshold_ticks", 4)) * tk
    mr = int(params.get("max_retest_bars", 36))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts = bars["ts"].values
    pdh = pd.Series(h).rolling(96, min_periods=20).max().shift(1).values
    pdl = pd.Series(l).rolling(96, min_periods=20).min().shift(1).values
    try:
        hr = pd.to_datetime(ts, utc=True, errors="coerce").hour.values
    except: return None
    L = n - 1
    if L < 5 or not (13 <= hr[L] < 20): return None
    if np.isnan(pdh[L]) or np.isnan(pdl[L]): return None
    if h[L] >= pdh[L] + bt:
        for j in range(max(0, L-mr), L):
            if c[j] < pdh[j] and l[j] >= pdl[j]:
                for k in range(j+1, L):
                    if l[k] > h[k-2]:
                        e = k+1
                        if e < n:
                            en = o[e]
                            st = min(l[j], l[L]) - sb
                            rk = en - st
                            if rk >= 0.5*tk:
                                return {"ts": ts[e], "direction": "long", "entry": en, "stop": st, "target": en + rk*tr}
                break
    if l[L] <= pdl[L] - bt:
        for j in range(max(0, L-mr), L):
            if c[j] > pdl[j] and h[j] <= pdh[j]:
                for k in range(j+1, L):
                    if h[k] < l[k-2]:
                        e = k+1
                        if e < n:
                            en = o[e]
                            st = max(h[j], h[L]) + sb
                            rk = st - en
                            if rk >= 0.5*tk:
                                return {"ts": ts[e], "direction": "short", "entry": en, "stop": st, "target": en - rk*tr}
                break
    return None
