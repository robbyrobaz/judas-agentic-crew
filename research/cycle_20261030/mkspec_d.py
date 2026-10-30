"""STAGE D spec (2026-10-30 cycle, task #2893).

The library screen (outputs/honest_fixed_library_20261029.json) shows that
EVERY legacy family -- silver_bullet_pdh_pdl_retest, ifvg_midpoint_reversion,
ob_midpoint_reversion, judas_continuation -- COLLAPSES under the production
fill (BASE PF 1.9-6.0  ->  LIVE PF 0.40-0.97).  The ONLY family that survives
the honest fill is the market-entry SWEEP -> DISPLACEMENT CONTINUATION
(csid439/csid440/csid441): its entry IS the decision bar's close, so there is
no structural (already-closed-bar) price to retro-fill.

Stage D therefore widens THAT family onto every legal symbol/TF cell whose
archive is deep enough to carry an IS/OOS admission test, prioritising the
broker-confirmed symbols (MNQ +$779.50, MGC +$351.00, ZF +$656.29).

Selection stays honest: cells are ranked on IS ONLY, and the OOS window is
the admission test (n >= 20 AND PF > 1.15).
"""
from __future__ import annotations

import json
from pathlib import Path

C440 = "research/cycle_20261029c/codes/csid440.py"   # 15m market-entry
C441 = "research/cycle_20261029c/codes/csid441.py"   # 5m data-hygiene port

BASE = {"confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}


def cell(lb, ms, dk, rr):
    c = dict(BASE)
    c.update({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
    return c


def grid(lbs, mss, dks, rrs):
    return [cell(lb, ms, dk, rr)
            for lb in lbs for ms in mss for dk in dks for rr in rrs]


WIDE = grid((8, 12, 20), (0.30, 0.45, 0.60), (0.8, 1.0), (1.0, 1.5))     # 36
MID = grid((6, 8, 12), (0.30, 0.45), (0.8, 1.0), (1.0,))                # 12
HOUR = grid((12, 20, 36), (0.30, 0.45), (1.0,), (1.0, 1.5))             # 12

jobs = [
    # (label, symbol, tf, code, cells, splits)
    ("me441 MNQ1h", "MNQ", "1h", C441, grid((8, 12, 20), (0.30, 0.45, 0.60),
                                             (0.8, 1.0), (1.0, 1.5)),
     ("global_time",)),
    ("me440 MGC15m", "MGC", "15m", C440, WIDE, ("global_time", "per_segment")),
    ("me441 MGC5m", "MGC", "5m", C441, WIDE, ("global_time", "per_segment")),
    ("me440 MNQ15m", "MNQ", "15m", C440, MID, ("global_time", "per_segment")),
    ("me441 ZF5m", "ZF", "5m", C441, MID, ("global_time", "per_segment")),
    ("me440 ZF15m", "ZF", "15m", C440, MID, ("global_time", "per_segment")),
    ("me440 MGC1h", "MGC", "1h", C440, HOUR, ("global_time",)),
]

spec = []
for label, sym, tf, code, cells, splits in jobs:
    for split in splits:
        spec.append({"label": label, "symbol": sym, "tf": tf,
                     "code_file": code, "csid": 0, "split_mode": split,
                     "cells": cells})

out = Path("research/cycle_20261030/spec_d.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(spec, indent=1))
print(f"{len(spec)} jobs / {sum(len(j['cells']) for j in spec)} screen runs")
for j in spec:
    print(f"  {j['label']:16s} {j['symbol']}_{j['tf']:3s} {j['split_mode']:12s} "
          f"{len(j['cells'])} cells")
