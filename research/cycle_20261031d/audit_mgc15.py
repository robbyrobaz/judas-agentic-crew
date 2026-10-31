"""Cycle 20261031d: audit the MGC 15m session-gated market-entry sweep->displacement.

Questions:
  1. span + hygiene of the MGC 15m archive (placeholder bars? gaps?);
  2. per-month / per-day concentration of the edge for the candidate config
     (is the PF carried by 1-2 lucky days?);
  3. neighbourhood robustness (session window x disp_k x rr) -- a plateau is
     evidence, a spike is overfitting;
  4. trade-set overlap vs the already-active csid439 (all-day) so the
     candidate is honestly described as a SESSION SUBSET.
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


def hygiene(symbol: str, tf: str) -> pd.DataFrame:
    df = load_bars(symbol, tf, None)
    print(f"# {symbol} {tf} clean bars={len(df)} "
          f"{df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
    raw = pd.read_parquet(Path("data/bars_archive") / f"{symbol}_{tf}.parquet")
    print(f"   raw rows={len(raw)}")
    d = df["ts"].diff().dt.total_seconds().value_counts().head(6)
    print("   bar-gap histogram (s):", dict(d))
    # duplicate timestamps?
    dup = int(df["ts"].duplicated().sum())
    print("   duplicate timestamps:", dup)
    zero = int((df["high"] <= df["low"]).sum())
    print("   zero-range bars left:", zero)
    return df


def per_month(bars, sigs, symbol):
    ts = bars["ts"]
    rows = []
    for ym, grp in sigs.items():
        pass
    buckets = defaultdict(list)
    for i, v in sigs.items():
        buckets[ts.iloc[i].strftime("%Y-%m")].append(i)
    out = []
    for k in sorted(buckets):
        idx = buckets[k]
        sub = {i: sigs[i] for i in idx}
        r = simulate_prod(bars, sub, symbol)
        out.append((k, r["n_trades"], r["pf"], r["total_pnl"]))
    return out


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    bars = hygiene(symbol, tf)

    grid = {
        "cand_sess6-12_disp0.8": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8},
        "cand_sess6-12_disp1.0": {"sess_lo": 6, "sess_hi": 12, "disp_k": 1.0},
        "cand_sess4-10": {"sess_lo": 4, "sess_hi": 10},
        "cand_sess5-12": {"sess_lo": 5, "sess_hi": 12},
        "cand_sess6-11": {"sess_lo": 6, "sess_hi": 11},
        "cand_sess6-13": {"sess_lo": 6, "sess_hi": 13},
        "cand_sess7-12": {"sess_lo": 7, "sess_hi": 12},
        "cand_sess6-12_rr2.0": {"sess_lo": 6, "sess_hi": 12, "rr": 2.0},
        "cand_sess6-12_ms0.45": {"sess_lo": 6, "sess_hi": 12, "min_sweep_atr": 0.45},
        "cand_sess6-12_lb20": {"sess_lo": 6, "sess_hi": 12, "lookback": 20},
        "cand_sess6-12_body0.45": {"sess_lo": 6, "sess_hi": 12, "body_ratio_min": 0.45},
        "cand_sess6-12_body0.65": {"sess_lo": 6, "sess_hi": 12, "body_ratio_min": 0.65},
        "cand_sess6-12_disp0.8_body0.45": {"sess_lo": 6, "sess_hi": 12, "disp_k": 0.8,
                                            "body_ratio_min": 0.45},
        "all_day_rr1.5": {},
    }
    summary = {}
    for name, over in grid.items():
        cfg = dict(BASE, **over)
        sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
        sc = wf_score(bars, sigs, symbol)
        full = simulate_prod(bars, sigs, symbol)
        summary[name] = {"over": over, "wf": sc, "full": full}
        print(f"{name:<30} minPF={sc['min_pf']:.3f} meanPF={sc['mean_pf']:.3f} "
              f"nOOS={sc['n_total']:4d} allpos={sc['all_pos']} "
              f"full_pf={full['pf']:.3f} full_n={full['n_trades']:4d} "
              f"full_pnl=${full['total_pnl']:>9.2f} "
              f"folds={[f['pf'] for f in sc['folds']]}")
        print(f"    fold windows: {[f['test'] for f in sc['folds']]}")

    # per-month concentration for the primary candidate
    primary = "cand_sess6-12_disp0.8"
    cfg = dict(BASE, **grid[primary])
    sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
    print(f"\n== per-month for {primary}")
    for m, n, pf, pnl in per_month(bars, sigs, symbol):
        print(f"   {m}  n={n:3d} pf={pf:7.3f} pnl=${pnl:>9.2f}")

    # overlap with the all-day variant (already active as csid439)
    allsig = sigs_sweep_disp(bars, dict(BASE, _tf=tf))
    inter = set(sigs) & set(allsig)
    print(f"\n== overlap with all-day csid439 config: {len(inter)} of {len(sigs)} "
          f"session-gated signals are also all-day signals "
          f"({100.0*len(inter)/max(1,len(sigs)):.1f}%)")

    with open(f"research/cycle_20261031d/audit_{symbol}_{tf}.json", "w") as fh:
        json.dump(summary, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
