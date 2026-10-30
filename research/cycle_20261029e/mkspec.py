"""Build the 2026-10-29e honest-fill job specs (task #2893).

Three stages:
  A  the MGC 15m market-entry sweep->displacement family -- the incumbent
     (csid439, active row 4998) vs the challenger (csid440) vs the 5m-hygiene
     code (csid441) at the same parameter points, so the collision guard has
     a measured incumbent envelope.
  B  the passing regions on MNQ 1h (broker-confirmed #1 symbol) and MCL 5m.
  C  the 2026-10-28 INCREMENTS (equal-pool + inducement mit50 + structure
     trend filter) and the two video-derived entries (MSS->FVG retest,
     sweep->iFVG inversion) on the cells where the base family failed.
"""
from __future__ import annotations

import json
from pathlib import Path

C440 = "research/cycle_20261029c/codes/csid440.py"
C439 = "research/cycle_20261029c/codes/csid439.py"
C441 = "research/cycle_20261029c/codes/csid441.py"
POOL = "research/cycle_20261028d/strategies/sweep_pool_filters_v2.py"
MSS = "research/cycle_20261028b/strategies/sweep_mss_fvg_liq_target_v2.py"
ZEN = "research/cycle_20261028/strategies/sweep_ifvg_inversion_zen_v1.py"

BASE = {"confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}


def cell(lb, ms, dk, rr, **kw):
    c = dict(BASE)
    c.update({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
    c.update(kw)
    return c


# ---------------------------------------------------------------- stage A ---
# MGC 15m.  The IS-selected point of the earlier grid is (8, 0.45, 1.0, 1.5)
# -- csid440's own DEFAULTS.  The incumbent active row 4998 runs csid439 at
# (12, 0.3, 1.0, 1.5).  Both are measured on both split protocols.
MGC15_CELLS = [
    cell(8, 0.45, 1.0, 1.5),   # challenger default / IS-selected
    cell(8, 0.45, 1.2, 1.5),   # neighbour (disp_k up)
    cell(8, 0.45, 0.8, 1.5),   # neighbour (disp_k down)
    cell(8, 0.45, 1.0, 1.0),   # neighbour (rr down)
    cell(8, 0.60, 1.0, 1.5),   # neighbour (sweep depth up)
    cell(12, 0.45, 1.0, 1.5),  # neighbour (lookback up)
    cell(12, 0.30, 1.0, 1.5),  # THE INCUMBENT (active row 4998 / csid439)
    cell(20, 0.60, 1.0, 2.0),  # the 5m cell, ported
]

stage_a = []
for split in ("global_time", "per_segment"):
    stage_a.append({"label": "me440 MGC15m", "symbol": "MGC", "tf": "15m",
                    "code_file": C440, "csid": 440, "split_mode": split,
                    "cells": MGC15_CELLS})
    stage_a.append({"label": "me439 MGC15m", "symbol": "MGC", "tf": "15m",
                    "code_file": C439, "csid": 439, "split_mode": split,
                    "cells": MGC15_CELLS})

# ---------------------------------------------------------------- stage B ---
MNQ1H_CELLS = [
    cell(lb, ms, dk, rr)
    for lb in (6, 8, 12, 16)
    for (ms, dk, rr) in ((0.30, 0.8, 1.0), (0.30, 1.0, 1.0), (0.45, 0.8, 1.0))
]

MCL5M_CELLS = [
    cell(lb, ms, dk, rr)
    for lb in (8, 12)
    for ms in (0.30, 0.45, 0.60)
    for dk in (1.0, 1.2)
    for rr in (1.0, 1.5)
]

stage_b = []
for split in ("global_time", "per_segment"):
    stage_b.append({"label": "sweep441 MNQ1h", "symbol": "MNQ", "tf": "1h",
                    "code_file": C441, "csid": 441, "split_mode": split,
                    "cells": MNQ1H_CELLS})
    stage_b.append({"label": "sweep441 MCL5m", "symbol": "MCL", "tf": "5m",
                    "code_file": C441, "csid": 441, "split_mode": split,
                    "cells": MCL5M_CELLS})

# ---------------------------------------------------------------- stage C ---
# MGC 5m with the equal-pool + inducement (mit50) filters -- a different
# architecture from the promoted csid441 base (active row 4999).
POOL_BASE = {"setup": "sweep_cont", "pool_mode": "rolling", "ext_window": 0,
             "prom_atr": 0.0, "prom_win": 20, "trend_mode": 0, "pivot_len": 2,
             "mit50": 0, "cluster_atr": 0.15, "min_pivots": 2,
             "cluster_age": 40, "confirm_bars": 6, "body_ratio_min": 0.55,
             "stop_buf_atr": 0.15, "atr_period": 14}


def pool_cell(pool_mode, mit50, lb, ms, dk, rr, trend=0):
    c = dict(POOL_BASE)
    c.update({"pool_mode": pool_mode, "mit50": mit50, "trend_mode": trend,
              "lookback": lb, "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
    return c


MGC5M_POOL = [pool_cell(pm, mi, 20, 0.60, dk, 2.0)
               for pm in ("rolling", "equal") for mi in (0, 1)
               for dk in (1.0, 1.2)]

# The two video-derived entries on the cells where the base family failed.
MSS_CELLS = [
    {"sweep_lookback": 20, "min_sweep_atr": ms, "disp_k": 0.6,
     "body_ratio_min": 0.45, "fvg_expiry": 10, "stop_buf_atr": 0.10,
     "atr_period": 14, "target_mode": "liquidity", "min_rr": 1.0}
    for ms in (0.20, 0.35)
]
ZEN_CELLS = [
    {"liq_lb": 20, "sweep_lookback": 40, "pierce_atr": 0.0, "mss_win": 8,
     "mss_lb": 10, "fvg_min_atr": 0.10, "fvg_expiry": 12,
     "require_fvg_tap": 1, "max_r": 2.0, "min_r": 0.8, "buf_atr": 0.15,
     "atr_period": 14},
    {"liq_lb": 20, "sweep_lookback": 40, "pierce_atr": 0.0, "mss_win": 8,
     "mss_lb": 10, "fvg_min_atr": 0.10, "fvg_expiry": 12,
     "require_fvg_tap": 0, "max_r": 2.0, "min_r": 0.8, "buf_atr": 0.15,
     "atr_period": 14},
]

stage_c = []
for split in ("global_time", "per_segment"):
    stage_c.append({"label": "pool MGC5m", "symbol": "MGC", "tf": "5m",
                    "code_file": POOL, "csid": 0, "split_mode": split,
                    "cells": MGC5M_POOL})
    for sym, tf in (("MNQ", "5m"), ("MNQ", "15m"), ("MCL", "5m")):
        stage_c.append({"label": f"mss {sym}{tf}", "symbol": sym, "tf": tf,
                        "code_file": MSS, "csid": 0, "split_mode": split,
                        "cells": MSS_CELLS})
        stage_c.append({"label": f"zen {sym}{tf}", "symbol": sym, "tf": tf,
                        "code_file": ZEN, "csid": 0, "split_mode": split,
                        "cells": ZEN_CELLS})
    # equal-pool + structure-trend filter on the cells the base family failed
    for sym, tf, lb, ms, dk, rr in (("MNQ", "5m", 8, 0.45, 1.0, 1.5),
                                    ("MNQ", "15m", 8, 0.30, 1.0, 1.5),
                                    ("MCL", "5m", 8, 0.45, 1.2, 1.5)):
        stage_c.append({"label": f"pooltrend {sym}{tf}", "symbol": sym, "tf": tf,
                        "code_file": POOL, "csid": 0, "split_mode": split,
                        "cells": [pool_cell("rolling", 1, lb, ms, dk, rr, 1),
                                  pool_cell("rolling", 0, lb, ms, dk, rr, 1),
                                  pool_cell("equal", 1, lb, ms, dk, rr, 1)]})

if __name__ == "__main__":
    out = Path("research/cycle_20261029e")
    out.mkdir(parents=True, exist_ok=True)
    for name, spec in (("spec_a.json", stage_a), ("spec_b.json", stage_b),
                       ("spec_c.json", stage_c)):
        (out / name).write_text(json.dumps(spec, indent=1))
        n = sum(len(j["cells"]) for j in spec)
        print(f"{name}: {len(spec)} jobs / {n} screen runs")
