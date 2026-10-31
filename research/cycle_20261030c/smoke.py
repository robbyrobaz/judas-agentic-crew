"""Local smoke test: feed synthetic bars to the DOL strategy and make sure
evaluate() never raises and returns sane signals."""
import importlib.util
import sys

import numpy as np
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "research/cycle_20261030c/dol_draw_target_v1.py"
spec = importlib.util.spec_from_file_location("m", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

rng = np.random.default_rng(7)
n = 2000
ts = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
price = 4000.0
rows = []
for i in range(n):
    o = price
    c = o + rng.normal(0, 3)
    h = max(o, c) + abs(rng.normal(0, 2))
    l = min(o, c) - abs(rng.normal(0, 2))
    rows.append((ts[i], o, h, l, c, max(1, int(rng.integers(50, 500)))))
    price = c
bars = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])

sigs = []
for i in range(300, n):
    sig = m.evaluate(bars.iloc[: i + 1], {})
    if sig is not None:
        sigs.append((i, sig["direction"], sig["entry"], sig["stop"], sig["target"]))
print("signals:", len(sigs))
for s in sigs[:8]:
    print(s)
if sigs:
    i, d, e, s_, t = sigs[0]
    assert s_ < e < t if d == "long" else s_ > e > t
    print("geometry OK")
