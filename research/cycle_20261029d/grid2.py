"""DEFINITIVE HONEST-FILL GRID (task #2893) -- 2026-10-29d.

WHY THIS FILE
-------------
`run_custom_backtest` (the house engine) resolves an entry from a bar that has
already CLOSED (retro-fill, finding 07f3a4c2).  Every PF envelope it stamps
is therefore suspect: csid229 PF 5.044 -> 0.643 and csid368 PF 6.325 -> 1.054
under a fair decision-time entry.  Coder autofix #565 failed.  So this cycle
measures EVERYTHING with the researcher's own harness:

    FILL   = `live` book: MARKET at the OPEN of the bar AFTER the decision
             bar, brackets re-anchored to the fill, gap-aware exits.
             This is the only fill the broker can give (there is no LIMIT
             entry path in this repo -- finding #9134).
    COSTS  = src.tools.cost_model.apply_costs_to_trade,
             COST_MODEL_VERSION = "v1_realistic_micros", DOLLAR-denominated,
             qty as intended (MGC $10/pt, MNQ $2/pt, MCL $10/pt, ZF $1000/pt).
    DATA   = per CLEAN SEGMENT (placeholder rows dropped), so a trade can
             never jump a time hole.
    SPLIT  = BOTH protocols are reported for every cell:
               "global_time"  one boundary at 60% of the global clean span
               "per_segment"  every segment split 60/40 on its own bar index
             A cell that only survives one protocol is not a cell.
    ADMISSION (the OOS test) = OOS n >= 20 AND OOS live PF > 1.15.
    RANKING = on IS ONLY.  OOS is never used to pick a cell.
    BT-LURE = flagged when PF > 3.0 AND WR > 70% (the shape that collapsed
               to PF 0.70-0.90 under an executable fill).

Usage:
    .venv/bin/python research/cycle_20261029d/grid2.py <spec.json> <out.json>
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from segment_honest_screen import screen  # noqa: E402

TICK = {"MGC": 0.1, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 5e-07}

# sweep -> displacement continuation (the family that survived the audit)
SWEEP_BASE = {"lookback": 20, "min_sweep_atr": 0.6, "disp_k": 1.0, "rr": 2.0,
              "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
              "atr_period": 14}


def sweep_cells():
    cells = []
    for lb, ms, dk, rr in itertools.product(
            (8, 12, 20), (0.30, 0.45, 0.60), (0.8, 1.0, 1.2), (1.0, 1.5, 2.0)):
        c = dict(SWEEP_BASE)
        c.update({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
        cells.append(c)
    return cells


def filter_cells():
    """sweep_pool_filters_v2 -- the 2026-10-28 increments: pool_mode, the
    structure-trend filter (trend_mode=1, HH-HL vs LH-LL) and the inducement
    mit50 filter, on top of the sweep->displacement base."""
    base = {"setup": "sweep_cont", "pool_mode": "rolling", "lookback": 20,
            "ext_window": 0, "prom_atr": 0.0, "prom_win": 20, "trend_mode": 0,
            "pivot_len": 2, "mit50": 0, "cluster_atr": 0.15, "min_pivots": 2,
            "cluster_age": 40, "min_sweep_atr": 0.60, "disp_k": 1.0,
            "body_ratio_min": 0.55, "rr": 2.0, "confirm_bars": 6,
            "stop_buf_atr": 0.15, "atr_period": 14}
    cells = []
    for pm, tm, mi, lb, ms, rr in itertools.product(
            ("rolling", "equal"), (0, 1), (0, 1), (8, 12, 20),
            (0.45, 0.60), (1.5, 2.0)):
        c = dict(base)
        c.update({"pool_mode": pm, "trend_mode": tm, "mit50": mi,
                  "lookback": lb, "min_sweep_atr": ms, "rr": rr})
        cells.append(c)
    return cells


CELL_BUILDERS = {"sweep": sweep_cells, "filters": filter_cells}


def _one(job):
    job, cell = job
    code = Path(job["code_file"]).read_text()
    params = dict(cell)
    params["tick"] = TICK[job["symbol"]]
    r = screen(code, job["symbol"], job["tf"], params,
               qty=job.get("qty", 1), mask=job.get("mask", "fixed"),
               budget_s=float(job.get("budget_s", 300)),
               split_mode=job.get("split_mode", "global_time"))
    r.pop("_live_trades", None)
    r["cell"] = cell
    r["label"] = job.get("label", "")
    r["csid"] = job.get("csid")
    r["code_file"] = job["code_file"]
    r["split_mode"] = job.get("split_mode", "global_time")
    return r


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    all_res = []
    for job in spec:
        builder = CELL_BUILDERS[job.get("cells_kind", "sweep")]
        cells = job.get("cells") or builder()
        jobs = [(job, c) for c in cells]
        res = []
        with ProcessPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
            for r in ex.map(_one, jobs):
                res.append(r)
        res.sort(key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                -(r["live_IS"].get("n") or 0)))
        print(f"\n### {job['label']} {job['symbol']}_{job['tf']} "
              f"[{job.get('split_mode','global_time')}] ({len(res)} cells, IS-ranked)")
        for r in res[:8]:
            c = r["cell"]
            li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
            oos_ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
            lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
            print(f"  {'PASS' if oos_ok else 'fail'}{' LURE' if lure else '    '}"
                  f" IS n={li.get('n',0):>4} PF={li.get('pf',0):>6.3f} | "
                  f"OOS n={lo.get('n',0):>4} PF={lo.get('pf',0):>6.3f} | "
                  f"ALL n={lv.get('n',0):>4} PF={lv.get('pf',0):>6.3f} "
                  f"WR={lv.get('wr_pct',0):>5} ${lv.get('total_usd',0):>8.0f} "
                  f"DD=${lv.get('max_dd',0):>7.0f}  {c}")
        all_res += res
        out_path.write_text(json.dumps(all_res, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
