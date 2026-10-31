"""Stage-by-stage diagnostic for the DOL strategy: how many bars survive
each filter.  Proves the pipeline can fire before spending backtest calls."""
import importlib.util
import sys

import numpy as np
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "research/cycle_20261030c/dol_draw_target_v1.py"
spec = importlib.util.spec_from_file_location("m", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

rng = np.random.default_rng(11)
n = 3000
ts = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
price = 4000.0
rows = []
for i in range(n):
    o = price
    c = o + rng.normal(0, 4)
    h = max(o, c) + abs(rng.normal(0, 3))
    l = min(o, c) - abs(rng.normal(0, 3))
    rows.append((ts[i], o, h, l, c, max(1, int(rng.integers(50, 500)))))
    price = c
bars = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])

# --- replicate the internals to count stages -------------------------
H = bars["high"].to_numpy(float); L = bars["low"].to_numpy(float)
C = bars["close"].to_numpy(float); O = bars["open"].to_numpy(float)
tarr = bars["ts"].to_numpy()
day = tarr.astype("datetime64[D]").astype("int64")
hour = tarr.astype("datetime64[h]").astype("int64") % 24
a = m._atr(H, L, C, 14)

cnt = dict(side=0, draw_live=0, asia=0, window=0, disp=0, fvg=0, retrace=0, sweep=0, rr=0)
for cur in range(300, n):
    last_day = day[cur]
    pds = np.unique(day[day < last_day])
    if len(pds) < 2:
        continue
    pd1, pd2 = int(pds[-1]), int(pds[-2])
    s1 = m._sess(H, L, C, day, pd1); s2 = m._sess(H, L, C, day, pd2)
    if s1 is None or s2 is None:
        continue
    p1h, p1l, p1c = s1; p2h, p2l, p2c = s2
    rng1 = max(p1h - p1l, 1e-9)
    side = None
    if p1h > p2h and p1c > p2h and p1c >= p1h - 0.25 * rng1:
        side = "high"
    elif p1h > p2h and p1c < p2h:
        side = "low"
    elif p1l < p2l and p1c < p2l and p1c <= p1l + 0.25 * rng1:
        side = "low"
    elif p1l < p2l and p1c > p2l:
        side = "high"
    if side is None:
        continue
    cnt["side"] += 1
    ls = side == "high"
    draw = p1h if ls else p1l
    tod = np.where(day == last_day)[0]
    if (float(H[tod].max()) >= draw) if ls else (float(L[tod].min()) <= draw):
        continue
    cnt["draw_live"] += 1
    asia = (day == last_day) & (hour >= 0) & (hour < 7)
    ai = np.where(asia)[0]
    if len(ai) < 4:
        continue
    cnt["asia"] += 1
    if not (6 <= hour[cur] < 20):
        continue
    cnt["window"] += 1
    lo_d = max(3, cur - 40)
    for d in range(cur, lo_d - 1, -1):
        rg = float(H[d] - L[d])
        if rg < 1.0 * a:
            continue
        if abs(float(C[d] - O[d])) < 0.55 * rg:
            continue
        cnt["disp"] += 1
        if ls:
            if not (float(L[d]) > float(H[d - 2])):
                continue
            zb, zt = float(H[d - 2]), float(L[d])
        else:
            if not (float(H[d]) < float(L[d - 2])):
                continue
            zb, zt = float(H[d]), float(L[d - 2])
        if (zt - zb) < 0.05 * a:
            continue
        cnt["fvg"] += 1
        if ls:
            if not (float(L[cur]) <= zt and float(L[cur]) >= zb and float(C[cur]) >= zb):
                continue
        else:
            if not (float(H[cur]) >= zb and float(H[cur]) <= zt and float(C[cur]) <= zt):
                continue
        cnt["retrace"] += 1
        w0 = max(0, d - 48)
        if ls:
            sl = float(L[w0:d].min())
            if not (sl <= float(L[ai].min()) - 0.25 * a):
                continue
        else:
            sh = float(H[w0:d].max())
            if not (sh >= float(H[ai].max()) + 0.25 * a):
                continue
        cnt["sweep"] += 1
        cnt["rr"] += 1
        break

for k, v in cnt.items():
    print(f"{k:10s} {v}")
