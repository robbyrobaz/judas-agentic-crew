"""DRIFT ATTRIBUTION: is a LONG-ONLY result EDGE or BETA? (2026-10-27 cycle)

WHY THIS FILE
-------------
The segment screen (research/cycle_20261027/seg_sweep.py) found 22 MCL 15m
survivors, most of them `disp_pullback` -- and `disp_pullback` in
research/cycle_20261025/strategies/market_entry_ict_v1.py is LONG-ONLY.
MCL rose hard over the window, so a long-only system that looks good there may
be BETA, not edge.

THE TEST: if the strategy is profitable in buckets where the market FELL, it
has something beyond beta.  If it only wins in rising buckets, it is beta
and must NOT be proposed.

FIXES vs the first version of this script (2026-10-27 02:37):
  * it read `market_entry_disp_pb_hygiene_v1.py` RAW -- that file is a
    TEMPLATE (it contains the marker `# --- CELL ---` and refers to a global
    CELL that is never defined), so every evaluate() call raised NameError
    and the "MCL disp_pullback LONG-only" line reported 8787 eval_errors and
    zero trades.  Now the script uses the concrete code that the segment
    screen actually used.
  * it ran on the RAW archive (dirty rows included).  The MCL/ZF/6J archives
    are 19-44% placeholder rows (zero range, zero volume) and a zero-range
    bar fabricates a sweep that never printed.  Now it runs per CLEAN
    SEGMENT, exactly like seg_sweep.py, and aggregates the trades.

FILLS: the `live` book -- fill at the OPEN of the bar AFTER the decision bar,
brackets re-anchored, gap-aware exits (production semantics, finding #9134).

Usage:
  .venv/bin/python research/cycle_20261027/drift_attribution.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from honest_screen import run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

ICT = (ROOT / "research" / "cycle_20261025" / "strategies" /
       "market_entry_ict_v1.py").read_text()
SWEEP_HYGIENE = (ROOT / "research" / "cycle_20261027" / "strategies" /
                 "market_entry_sweep_cont_hygiene_v1.py").read_text()

# the exact survivor cells from outputs/seg_sweep_{MCL,MNQ}_15m_20261027.json
MCL_CELL = {"setup": "disp_pullback", "lookback": 12, "pullback_bars": 8,
            "retrace": 0.5, "rr": 1.0, "disp_k": 1.0, "body_ratio_min": 0.55,
            "stop_buf_atr": 0.15, "atr_period": 14}
MCL_CELL_SWEEP = {"setup": "sweep_cont", "lookback": 32, "min_sweep_atr": 0.15,
                  "disp_k": 0.8, "rr": 1.0, "confirm_bars": 6,
                  "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
                  "atr_period": 14}
SWEEP_CELL = {"lookback": 8, "min_sweep_atr": 0.45, "disp_k": 1.0, "rr": 1.5,
              "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
              "atr_period": 14}


def stats(trades):
    if not trades:
        return {"n": 0, "pf": 0.0, "pnl": 0.0, "e_r": 0.0}
    win = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    return {"n": len(trades), "pf": round(win / loss, 3) if loss > 0 else None,
            "pnl": round(sum(t["pnl"] for t in trades), 2),
            "e_r": round(sum(t["r"] for t in trades) / len(trades), 3)}


def run(symbol, code, cell, label, n_buckets=4, tf="15m"):
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol}: no clean segment")
        return None
    # aggregate the live book over all clean segments, keeping the bar index
    # of the segment each trade came from (for the market-drift calc)
    all_t = []
    closes, tss = [], []
    for seg in segs:
        _, live_t, _, meta = run_books(code, seg, cell, symbol, budget_s=600.0)
        if meta.get("load_error"):
            print(f"{symbol} {label}: load error {meta}")
            return None
        if meta.get("eval_errors"):
            print(f"   !! {symbol} {label}: {meta['eval_errors']} eval errors")
        off = sum(len(c) for c in closes)
        for t in live_t:
            t = dict(t)
            t["entry_bar"] = t["entry_bar"] + off
            all_t.append(t)
        closes.append(seg["close"].to_numpy(float))
        tss.append(seg["ts"])
    if not all_t:
        print(f"{symbol} {label}: no live trades")
        return None
    C = np.concatenate(closes)
    TS = pd.concat(tss, ignore_index=True)
    tr = np.maximum(np.abs(np.diff(C, prepend=C[0])),
                    np.zeros(len(C)))
    hi = np.concatenate([s["high"].to_numpy(float) for s in segs])
    lo = np.concatenate([s["low"].to_numpy(float) for s in segs])
    true_range = np.maximum(hi[1:] - lo[1:],
                            np.maximum(np.abs(hi[1:] - C[:-1]),
                                       np.abs(lo[1:] - C[:-1])))
    atr = float(true_range.mean()) if len(true_range) else 1.0

    print(f"== {symbol} {label}  live n={len(all_t)} overall "
          f"{stats(all_t)}")
    edges = np.array_split(np.arange(len(all_t)), n_buckets)
    out = []
    for bi, idx in enumerate(edges):
        if len(idx) == 0:
            continue
        sub = [all_t[i] for i in idx]
        b0 = sub[0]["entry_bar"]
        b1 = sub[-1]["entry_bar"]
        drift_atr = (C[min(b1 + 1, len(C) - 1)] - C[b0]) / (atr or 1.0)
        s = stats(sub)
        print(f"   bucket {bi}: {TS.iloc[b0]} -> {TS.iloc[b1]}  "
              f"market drift={drift_atr:+8.1f} ATR | strat n={s['n']:3d} "
              f"pf={s['pf']} pnl={s['pnl']:>9,.0f} e_r={s['e_r']:+.3f}")
        out.append({"bucket": bi, "start": str(TS.iloc[b0]), "end": str(TS.iloc[b1]),
                    "market_drift_atr": round(drift_atr, 1), **s})
    return out


def main():
    res = {}
    res["MCL_disp_pb_longonly"] = run("MCL", ICT, MCL_CELL, "disp_pullback LONG-only")
    res["MCL_sweep_cont"] = run("MCL", ICT, MCL_CELL_SWEEP, "sweep_cont both")
    res["MGC_sweep_cont"] = run("MGC", SWEEP_HYGIENE, SWEEP_CELL, "sweep_cont both")
    res["MNQ_sweep_cont"] = run("MNQ", SWEEP_HYGIENE, SWEEP_CELL, "sweep_cont both")
    (ROOT / "outputs" / "drift_attribution_20261027.json").write_text(
        json.dumps(res, indent=2, default=str))
    print("\nwrote outputs/drift_attribution_20261027.json")


if __name__ == "__main__":
    main()
