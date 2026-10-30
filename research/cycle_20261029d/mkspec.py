"""Spec for the 2026-10-29d honest-fill grid (task #2893).

Batches:
  A  sweep -> displacement continuation (csid441 code, the family that
     survived the honest-fill audit), on the cells the earlier grids did not
     cover, in BOTH split protocols.
  B  the 2026-10-28 INCREMENTS (structure-trend filter `trend_mode`, the
     inducement `mit50` filter, the equal-pool mode) on top of the same base
     architecture, via research/cycle_20261028d/strategies/sweep_pool_filters_v2.py
  C  the two remaining increments at their defaults (cheap screen first):
     C1 sweep -> MSS -> FVG retest (YT:NRwsJ9NVH5c)
     C2 sweep -> HTF-FVG tap -> iFVG inversion (YT:pmexdsocN9c)
"""
import json
from pathlib import Path

C441 = "research/cycle_20261029c/codes/csid441.py"
FILT = "research/cycle_20261028d/strategies/sweep_pool_filters_v2.py"
MSS = "research/cycle_20261028b/strategies/sweep_mss_fvg_liq_target_v2.py"
ZEN = "research/cycle_20261028/strategies/sweep_ifvg_inversion_zen_v1.py"

spec = []

# ---------------------------------------------------------------- batch A ---
for sym, tf, split in [("MGC", "15m", "per_segment"),
                       ("MCL", "5m", "per_segment"),
                       ("MCL", "15m", "global_time"),
                       ("MCL", "15m", "per_segment"),
                       ("MGC", "5m", "global_time"),
                       ("ZF", "15m", "global_time")]:
    spec.append({"csid": 441, "label": f"sweep_cont {sym}{tf}",
                 "symbol": sym, "tf": tf, "code_file": C441,
                 "split_mode": split, "cells_kind": "sweep"})

# ---------------------------------------------------------------- batch B ---
for sym, tf, split in [("MGC", "15m", "global_time"),
                       ("MGC", "15m", "per_segment"),
                       ("MGC", "5m", "global_time"),
                       ("MGC", "5m", "per_segment"),
                       ("MNQ", "15m", "global_time"),
                       ("MNQ", "5m", "global_time")]:
    spec.append({"csid": 0, "label": f"filters {sym}{tf}",
                 "symbol": sym, "tf": tf, "code_file": FILT,
                 "split_mode": split, "cells_kind": "filters"})

# ---------------------------------------------------------------- batch C ---
for sym, tf in [("MGC", "15m"), ("MNQ", "15m"), ("MGC", "5m"), ("MNQ", "5m"),
                ("MGC", "1h"), ("MNQ", "1h")]:
    spec.append({"csid": 0, "label": f"mss_fvg {sym}{tf}",
                 "symbol": sym, "tf": tf, "code_file": MSS,
                 "split_mode": "global_time", "cells": [{}]})
for sym, tf in [("MGC", "15m"), ("MNQ", "15m"), ("MGC", "5m"), ("MNQ", "5m"),
                ("MGC", "1h"), ("MNQ", "1h")]:
    spec.append({"csid": 0, "label": f"zen_ifvg {sym}{tf}",
                 "symbol": sym, "tf": tf, "code_file": ZEN,
                 "split_mode": "global_time", "cells": [{}]})

Path("research/cycle_20261029d/spec.json").write_text(json.dumps(spec, indent=2))
print(f"{len(spec)} jobs")
for s in spec:
    print(" ", s["label"], s.get("split_mode"))
