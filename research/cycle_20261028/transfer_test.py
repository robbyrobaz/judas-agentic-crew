"""CROSS-SYMBOL TRANSFER TEST of the PROMOTED market-entry sweep->displacement
architecture (csid=439 family, MGC 15m, candidate #7382) onto the symbols the
architecture has NOT yet been validated on: ZF and 6J (plus MCL, and MGC/MNQ
as controls).

WHY THIS FILE
-------------
The hygiene port (research/cycle_20261027/strategies/
market_entry_sweep_cont_hygiene_v1.py) was run on MGC (live PF 1.722, n=71)
and MNQ (live PF 0.910, n=74) by finding 9153.  ZF and 6J were NEVER run with
the hygiene code under the honest `live` fill -- the only cross-port numbers
that exist for them (ZF PF 0.812 n=73, 6J PF 1.017 n=62) are PRE-hygiene,
i.e. they were computed on placeholder-polluted archives where a zero-range
bar fabricates a liquidity sweep that never printed.

The cell used here was selected on MGC (and survived a TRUE out-of-sample
holdout, candidate #7382), so running it on ZF/6J is a genuine
cross-symbol OUT-OF-SAMPLE test: no parameter is fitted to ZF/6J data here.

FILLS: the `live` book -- MARKET at the OPEN of the bar AFTER the decision
bar, brackets re-anchored, gap-aware exits (production semantics, #9134),
cost_model=v1_realistic_micros, qty=1.  This is the only fill the broker can
give us (there is no LIMIT entry path in this repo).

DATA: per CLEAN SEGMENT (placeholder rows dropped) so a trade can never jump
a time gap; trades from all segments are then aggregated.

Usage:
  .venv/bin/python research/cycle_20261028/transfer_test.py <SYMBOL> <tf>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

HYGIENE = (ROOT / "research" / "cycle_20261027" / "strategies" /
           "market_entry_sweep_cont_hygiene_v1.py").read_text()

# the cell promoted on MGC (candidate #7382 / finding 9153 SWEEP_CELL)
MGC_CELL = {"lookback": 8, "min_sweep_atr": 0.45, "disp_k": 1.0, "rr": 1.5,
            "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
            "atr_period": 14}

# small neighbourhood of the MGC cell -- NOT a search, a robustness check:
# if only the exact MGC cell works and its neighbours do not, the edge is a
# single-point fit and must not be promoted.
NEIGHBOURS = [
    {**MGC_CELL, "min_sweep_atr": 0.30},
    {**MGC_CELL, "min_sweep_atr": 0.60},
    {**MGC_CELL, "lookback": 12},
    {**MGC_CELL, "lookback": 20},
    {**MGC_CELL, "rr": 1.0},
    {**MGC_CELL, "rr": 2.0},
    {**MGC_CELL, "disp_k": 0.8},
    {**MGC_CELL, "disp_k": 1.2},
    {**MGC_CELL, "body_ratio_min": 0.70},
]


def run_cell(symbol, tf, code, cell, min_seg=300):
    segs = clean_segments(symbol, tf, min_seg=min_seg)
    if not segs:
        return None, {"error": "no clean segment"}
    all_live, all_fair, all_base = [], [], []
    nbars = 0
    span = []
    for seg in segs:
        b_t, l_t, f_t, meta = run_books(code, seg, cell, symbol, budget_s=900.0)
        if meta.get("load_error"):
            return None, meta
        all_base += b_t
        all_live += l_t
        all_fair += f_t
        nbars += meta.get("n_bars", 0)
        span.append((str(seg["ts"].iloc[0]), str(seg["ts"].iloc[-1])))
    out = {
        "symbol": symbol, "tf": tf, "cell": cell,
        "n_segments": len(segs), "n_bars": nbars,
        "segments": span[:12],
        "live": metrics(all_live), "fair": metrics(all_fair),
        "base": metrics(all_base),
    }
    return out, {}


def main():
    symbol = sys.argv[1].upper()
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    cells = [MGC_CELL] + NEIGHBOURS
    rows = []
    for cell in cells:
        res, meta = run_cell(symbol, tf, HYGIENE, cell)
        if res is None:
            print(f"{symbol} {tf} {cell} -> {meta}", flush=True)
            continue
        lv = res["live"]
        print(f"{symbol} {tf} cell={ {k: cell[k] for k in ('lookback','min_sweep_atr','disp_k','rr','body_ratio_min')} } "
              f"live n={lv['n']:4d} pf={lv['pf']} e_r={lv['e_r']} pnl={lv['total_usd']} | "
              f"fair n={res['fair']['n']} pf={res['fair']['pf']} | base n={res['base']['n']} pf={res['base']['pf']}",
              flush=True)
        rows.append(res)
    out = ROOT / "outputs" / f"transfer_{symbol}_{tf}_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
