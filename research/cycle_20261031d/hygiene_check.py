"""Cycle 20261031d -- session-gated market-entry sweep->displacement, honest fill.

Adds to the 20261031a harness:
  * STRICT hygiene (drop bars with range == 0, i.e. single-print buckets) as
    an OPTION, because the live scanner only ever sees bars that printed.
    The 15m archives carry 2940 (MGC) / 999 (MNQ) zero-range-but-volume>0
    bars; those deflate ATR and therefore change every ATR threshold.
  * a per-config neighbourhood (plateau vs spike) and per-month breakdown.

Scoring: 3-fold chronological walk-forward, a config only counts if EVERY
out-of-sample fold has PF > 1.0.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research/cycle_20261031a"))

from explore_prod import load_bars, sigs_sweep_disp, simulate_prod  # noqa: E402
from coord_search import BASE, wf_score  # noqa: E402


def strict_hygiene(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars[(bars["high"] - bars["low"]) > 0]
    if "volume" in b.columns:
        b = b[b["volume"] > 0]
    return b.reset_index(drop=True)


def per_month(bars, sigs, symbol):
    ts = bars["ts"]
    buckets = defaultdict(list)
    for i in sigs:
        buckets[ts.iloc[i].strftime("%Y-%m")].append(i)
    out = []
    for k in sorted(buckets):
        sub = {i: sigs[i] for i in buckets[k]}
        r = simulate_prod(bars, sub, symbol)
        out.append((k, r["n_trades"], r["pf"], r["total_pnl"]))
    return out


def run(symbol, tf, grid, hygiene):
    raw = load_bars(symbol, tf, None)
    bars = strict_hygiene(raw) if hygiene else raw
    print(f"\n##### {symbol} {tf} hygiene={hygiene} bars={len(bars)} "
          f"{bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")
    res = {}
    for name, over in grid.items():
        cfg = dict(BASE, **over)
        sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
        sc = wf_score(bars, sigs, symbol)
        full = simulate_prod(bars, sigs, symbol)
        res[name] = {"over": over, "wf": sc, "full": full}
        flag = "PASS" if sc["all_pos"] and sc["n_total"] >= 25 else "    "
        print(f"{flag} {name:<26} minPF={sc['min_pf']:.3f} meanPF={sc['mean_pf']:.3f} "
              f"nOOS={sc['n_total']:4d} full_pf={full['pf']:.3f} "
              f"full_n={full['n_trades']:4d} full_pnl=${full['total_pnl']:>9.2f} "
              f"folds={[f['pf'] for f in sc['folds']]}")
    return bars, res


MGC_GRID = {
    "sess6-12_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8},
    "sess6-12_disp1.0": {"sess_lo": 6, "sess_hi": 12, "disp_k": 1.0},
    "sess5-12_disp0.8": {"sess_lo": 5, "sess_hi": 12, "disp_k": 0.8},
    "sess6-11_disp0.8": {"sess_lo": 6, "sess_hi": 11, "disp_k": 0.8},
    "sess4-10_disp0.8": {"sess_lo": 4, "sess_hi": 10, "disp_k": 0.8},
    "sess6-12_disp1.2": {"sess_lo": 6, "sess_hi": 12, "disp_k": 1.2},
    "sess6-12_rr2.0_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8, "rr": 2.0},
    "sess6-12_rr1.0_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8, "rr": 1.0},
    "sess6-12_ms0.45_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8,
                                "min_sweep_atr": 0.45},
    "sess6-12_ms0.20_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8,
                                "min_sweep_atr": 0.20},
    "sess6-12_lb20_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8, "lookback": 20},
    "sess6-12_body0.65_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8,
                                  "body_ratio_min": 0.65},
    "all_day_rr1.5": {},
}

MNQ_GRID = {
    "sess7-12_rr1.0": {"sess_lo": 7, "sess_hi": 12, "rr": 1.0},
    "sess6-12_rr1.0": {"sess_lo": 6, "sess_hi": 12, "rr": 1.0},
    "sess6-12_rr1.0_disp0.8": {"sess_lo": 6, "sess_hi": 12, "rr": 1.0, "disp_k": 0.8},
    "sess6-12_rr1.0_body0.65": {"sess_lo": 6, "sess_hi": 12, "rr": 1.0,
                                "body_ratio_min": 0.65},
    "sess6-12_rr1.5": {"sess_lo": 6, "sess_hi": 12},
    "sess6-12_rr1.5_body0.65": {"sess_lo": 6, "sess_hi": 12, "body_ratio_min": 0.65},
    "sess7-13_rr1.0": {"sess_lo": 7, "sess_hi": 13, "rr": 1.0},
    "sess5-12_rr1.0": {"sess_lo": 5, "sess_hi": 12, "rr": 1.0},
    "all_day": {},
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    grid = MGC_GRID if which == "MGC" else MNQ_GRID
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    out = {}
    for hyg in (False, True):
        bars, res = run(which, tf, grid, hyg)
        out[f"hygiene_{hyg}"] = res
        if hyg:
            # per-month for the primary config of this symbol
            primary = ("sess6-12_disp0.8" if which == "MGC" else "sess7-12_rr1.0")
            cfg = dict(BASE, **grid[primary])
            sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
            print(f"\n== per-month {which} {primary} (hygiene)")
            for m, n, pf, pnl in per_month(bars, sigs, which):
                print(f"   {m}  n={n:3d} pf={pf:8.3f} pnl=${pnl:>9.2f}")
    with open(f"research/cycle_20261031d/hyg_{which}_{tf}.json", "w") as fh:
        json.dump(out, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
