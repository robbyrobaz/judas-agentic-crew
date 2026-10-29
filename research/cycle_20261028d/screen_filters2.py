"""FINE GRID around the two surviving MGC cells (2026-10-28d, grid 2).

usage: .venv/bin/python research/cycle_20261028d/screen_filters2.py <SYMBOL> <tf>
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


def grid_cells():
    cells = []
    for lb in (24, 30, 36, 40):
        cells.append((f"lb{lb}", _c(lookback=lb)))
    for ms in (0.45, 0.75):
        cells.append((f"lb30_ms{int(ms*100):03d}", _c(lookback=30, min_sweep_atr=ms)))
    for rr in (1.5, 2.5, 3.0):
        cells.append((f"lb30_rr{int(rr*10):02d}", _c(lookback=30, rr=rr)))
    for cb in (4, 10):
        cells.append((f"lb30_cb{cb}", _c(lookback=30, confirm_bars=cb)))
    for ext in (45, 60, 90):
        cells.append((f"ext{ext}", _c(ext_window=ext)))
    cells.append(("lb30_ext60", _c(lookback=30, ext_window=60)))
    cells.append(("lb30_ext90", _c(lookback=30, ext_window=90)))
    cells.append(("ext60_rr15", _c(ext_window=60, rr=1.5)))
    cells.append(("ext60_br050", _c(ext_window=60, body_ratio_min=0.50)))
    cells.append(("ext60_br065", _c(ext_window=60, body_ratio_min=0.65)))
    cells.append(("eq_ca008", _c(pool_mode="equal", cluster_atr=0.08)))
    cells.append(("eq_ca025", _c(pool_mode="equal", cluster_atr=0.25)))
    cells.append(("eq_mp3", _c(pool_mode="equal", min_pivots=3)))
    cells.append(("eq_age60", _c(pool_mode="equal", cluster_age=60)))
    cells.append(("eq_ca008_mp3", _c(pool_mode="equal", cluster_atr=0.08, min_pivots=3)))
    return cells


if __name__ == "__main__":
    sf.grid_cells = grid_cells
    sys.exit(sf.main())
