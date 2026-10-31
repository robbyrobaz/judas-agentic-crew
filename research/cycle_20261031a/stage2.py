"""Stage-2: combine the surviving knobs, test the new ASIAN-RANGE architecture,
and check transfer to the other legal symbols.

Scoring is the same 3-fold chronological walk-forward as the coordinate
search (worst test fold PF + mean test fold PF + total OOS trade count).
A config is only interesting if EVERY out-of-sample fold is positive.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coord_search import BASE, FOLDS, wf_score  # noqa: E402
from explore_prod import load_bars, sigs_sweep_disp, simulate_prod  # noqa: E402

CANDS = [
    ("baseline", {}),
    ("rr1.0", {"rr": 1.0}),
    ("sess6-12", {"sess_lo": 6, "sess_hi": 12}),
    ("rr1.0+sess6-12", {"rr": 1.0, "sess_lo": 6, "sess_hi": 12}),
    ("rr1.0+sess7-12", {"rr": 1.0, "sess_lo": 7, "sess_hi": 12}),
    ("sess5-12", {"sess_lo": 5, "sess_hi": 12}),
    ("sess6-11", {"sess_lo": 6, "sess_hi": 11}),
    ("sess6-13", {"sess_lo": 6, "sess_hi": 13}),
    ("sess7-13", {"sess_lo": 7, "sess_hi": 13}),
    ("sess4-10", {"sess_lo": 4, "sess_hi": 10}),
    ("sess6-12+ms0.45", {"sess_lo": 6, "sess_hi": 12, "min_sweep_atr": 0.45}),
    ("sess6-12+rr1.5+buf0.6", {"sess_lo": 6, "sess_hi": 12, "stop_buf_atr": 0.6}),
    ("sess6-12+lb8", {"sess_lo": 6, "sess_hi": 12, "lookback": 8}),
    ("sess6-12+lb20", {"sess_lo": 6, "sess_hi": 12, "lookback": 20}),
    ("sess6-12+disp0.8", {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8}),
    ("sess6-12+confirm10", {"sess_lo": 6, "sess_hi": 12, "confirm_bars": 10}),
    ("sess6-12+body0.45", {"sess_lo": 6, "sess_hi": 12, "body_ratio_min": 0.45}),
    ("sess6-12+body0.65", {"sess_lo": 6, "sess_hi": 12, "body_ratio_min": 0.65}),
    # NEW architecture: sweep of the ASIAN RANGE, traded market-entry on the
    # displacement inside the London AM window
    ("asian+sess6-12", {"sweep_ref": "asian", "sess_lo": 6, "sess_hi": 12}),
    ("asian+sess6-12+rr1.0", {"sweep_ref": "asian", "sess_lo": 6, "sess_hi": 12, "rr": 1.0}),
    ("asian+sess6-12+ms0.2", {"sweep_ref": "asian", "sess_lo": 6, "sess_hi": 12,
                              "min_sweep_atr": 0.2}),
    ("asian+sess6-16", {"sweep_ref": "asian", "sess_lo": 6, "sess_hi": 16}),
    ("asian+sess7-12", {"sweep_ref": "asian", "sess_lo": 7, "sess_hi": 12}),
    ("asian+sess6-12+confirm10", {"sweep_ref": "asian", "sess_lo": 6, "sess_hi": 12,
                                   "confirm_bars": 10}),
]


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    bars = load_bars(symbol, tf, None)
    print(f"# {symbol} {tf} bars={len(bars)} "
          f"{bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")
    out = []
    for name, over in CANDS:
        cfg = dict(BASE, **over)
        sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
        sc = wf_score(bars, sigs, symbol)
        full = simulate_prod(bars, sigs, symbol)
        flag = "PASS" if sc["all_pos"] and sc["n_total"] >= 25 else "    "
        print(f"{flag} {name:<26} minPF={sc['min_pf']:.3f} meanPF={sc['mean_pf']:.3f} "
              f"nOOS={sc['n_total']:4d} full_n={full['n_trades']:4d} "
              f"full_pf={full['pf']:.3f} full_pnl=${full['total_pnl']:>9.2f} "
              f"folds={[f['pf'] for f in sc['folds']]}")
        out.append({"name": name, "over": over, "wf": sc, "full": full})
    with open(f"research/cycle_20261031a/stage2_{symbol}_{tf}.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
