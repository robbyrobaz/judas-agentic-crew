"""GRID 3 -- the EXTERNAL-POOL axis (longer rolling pool window) + 15m ports.

usage: .venv/bin/python research/cycle_20261028d/screen_filters3.py <SYMBOL> <tf> <grid>
        grid in {pool5, pool15}
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

import screen_filters as sf  # noqa: E402

BASE = dict(sf.BASE)


def _c(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


def pool5():
    cells = []
    for lb in (40, 50, 60, 80):
        cells.append((f"lb{lb}", _c(lookback=lb)))
    for lb in (40, 50, 60):
        cells.append((f"lb{lb}_cb10", _c(lookback=lb, confirm_bars=10)))
        cells.append((f"lb{lb}_ms075", _c(lookback=lb, min_sweep_atr=0.75)))
    for rr in (1.5, 2.5):
        cells.append((f"lb40_rr{int(rr*10):02d}", _c(lookback=40, rr=rr)))
    for ext in (60, 90):
        cells.append((f"lb40_ext{ext}", _c(lookback=40, ext_window=ext)))
        cells.append((f"lb50_ext{ext}", _c(lookback=50, ext_window=ext)))
    cells.append(("lb36_cb4", _c(lookback=36, confirm_bars=4)))
    cells.append(("lb45", _c(lookback=45)))
    cells.append(("lb45_cb10", _c(lookback=45, confirm_bars=10)))
    return cells


def pool15():
    cells = []
    for lb in (12, 20, 30):
        for ms in (0.30, 0.60):
            cells.append((f"lb{lb}_ms{int(ms*100):03d}", _c(lookback=lb, min_sweep_atr=ms)))
    for lb in (12, 20, 30):
        cells.append((f"lb{lb}_ms030_rr15", _c(lookback=lb, min_sweep_atr=0.30, rr=1.5)))
        cells.append((f"lb{lb}_ms030_rr20", _c(lookback=lb, min_sweep_atr=0.30, rr=2.0)))
    for ext in (45, 60):
        cells.append((f"lb20_ext{ext}", _c(lookback=20, ext_window=ext)))
        cells.append((f"lb30_ext{ext}", _c(lookback=30, ext_window=ext)))
    cells.append(("lb20_ms030_cb10", _c(lookback=20, min_sweep_atr=0.30, confirm_bars=10)))
    cells.append(("lb30_ms030_cb10", _c(lookback=30, min_sweep_atr=0.30, confirm_bars=10)))
    return cells


if __name__ == "__main__":
    which = sys.argv[3] if len(sys.argv) > 3 else "pool5"
    sf.grid_cells = {"pool5": pool5, "pool15": pool15}[which]
    sys.exit(sf.main())
