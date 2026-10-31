"""Count why the session-gated code produces no signals."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

import numpy as np  # noqa: E402
from explore_prod import load_bars, sigs_sweep_disp  # noqa: E402
from coord_search import BASE  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261031a" /
        "market_entry_sweep_cont_london_15m_v1.py").read_text()
g = {}
exec(CODE, g)
evaluate = g["evaluate"]

bars = load_bars("MGC", "15m", 60)
N = len(bars)
hits = 0
sess_hits = 0
for i in range(100, N):
    sig = evaluate(bars.iloc[: i + 1], {})
    if sig is not None:
        hits += 1
        h = int(bars["ts"].iloc[i].hour)
        if 6 <= h < 12:
            sess_hits += 1
print(f"file evaluate: signals={hits} (in-session {sess_hits}) over {N} bars")

sigs = sigs_sweep_disp(bars, dict(BASE, _tf="15m"))
print("fast harness baseline signals:", len(sigs))
sess = {i: v for i, v in sigs.items() if 6 <= int(bars["ts"].iloc[i].hour) < 12}
print("fast harness baseline signals in 06-12 UTC:", len(sess))

# --- replicate the FILE logic with rejection counters --------------------
H = bars["high"].to_numpy(float)
L = bars["low"].to_numpy(float)
C = bars["close"].to_numpy(float)
O = bars["open"].to_numpy(float)
TAIL = 400
lb, confirm, ms, disp_k = 12, 6, 0.30, 1.0
min_body, buf, atr_period, rr = 0.55, 0.15, 14, 1.5
hours = bars["ts"].dt.hour.to_numpy()
cnt = {"sess": 0, "no_sweep": 0, "disp": 0, "ok": 0}
for cur in range(100, N):
    lo_i = max(0, cur - TAIL + 1)
    Hs, Ls, Cs, Os = H[lo_i:cur + 1], L[lo_i:cur + 1], C[lo_i:cur + 1], O[lo_i:cur + 1]
    c = len(Cs) - 1
    if not (6 <= hours[cur] < 12):
        cnt["sess"] += 1
        continue
    prev_c = Cs[:-1]
    tr = np.maximum(Hs[1:] - Ls[1:], np.maximum(np.abs(Hs[1:] - prev_c), np.abs(Ls[1:] - prev_c)))
    a = float(np.mean(tr[-atr_period:]))
    ccur, ocur = Cs[c], Os[c]
    rng = max(Hs[c] - Ls[c], 1e-9)
    br = abs(ccur - ocur) / rng
    j = -1
    for k in range(c - 1, max(0, c - confirm - 1), -1):
        if k < lb + 1:
            continue
        phk = float(np.max(Hs[k - lb:k]))
        plk = float(np.min(Ls[k - lb:k]))
        if Ls[k] < plk - ms * a and Cs[k] > plk:
            j = k
            break
        if Hs[k] > phk + ms * a and Cs[k] < phk:
            j = k
            break
    if j < 0:
        cnt["no_sweep"] += 1
        continue
    plj = float(np.min(Ls[j - lb:j]))
    phj = float(np.max(Hs[j - lb:j]))
    if Ls[j] < plj - ms * a and Cs[j] > plj:
        ok = (ccur > plj and br >= min_body and rng >= disp_k * a)
        direction = "long"
    else:
        ok = (ccur < phj and br >= min_body and rng >= disp_k * a)
        direction = "short"
    if not ok:
        cnt["disp"] += 1
        continue
    cnt["ok"] += 1
print("replication counters:", cnt)
