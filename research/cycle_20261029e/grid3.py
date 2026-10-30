"""Honest-fill grid runner for the 2026-10-29e cycle (task #2893).

Same measurement protocol as research/cycle_20261029d/grid2.py:

    FILL   = `live` book: MARKET at the OPEN of the bar AFTER the decision bar,
             brackets re-anchored to the fill, gap-aware exits (production
             semantics -- the live path submits a MKT parent bracket and
             re-anchors the distances to the actual fill).
    COSTS  = src.tools.cost_model.apply_costs_to_trade,
             COST_MODEL_VERSION = "v1_realistic_micros", dollar-denominated,
             qty as intended (MGC $10/pt, MNQ $2/pt, MCL $10/pt).
    DATA   = per CLEAN SEGMENT (placeholder rows dropped) so a trade can never
             jump a time hole.
    SPLIT  = BOTH protocols are reported for every cell:
               "global_time"  one boundary at 60% of the global clean span
               "per_segment"  every segment split 60/40 on its own bar index
    ADMISSION (the OOS test) = OOS n >= 20 AND OOS live PF > 1.15.
    RANKING = on IS ONLY.  OOS is never used to pick a cell.

Usage:
    .venv/bin/python research/cycle_20261029e/grid3.py <spec.json> <out.json>
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from segment_honest_screen import screen  # noqa: E402

TICK = {"MGC": 0.1, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 5e-07}


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
        cells = job.get("cells") or [{}]
        jobs = [(job, c) for c in cells]
        res = []
        with ProcessPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
            for r in ex.map(_one, jobs):
                res.append(r)
        res.sort(key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                -(r["live_IS"].get("n") or 0)))
        print(f"\n### {job['label']} {job['symbol']}_{job['tf']} "
              f"[{job.get('split_mode', 'global_time')}] ({len(res)} cells, IS-ranked)")
        for r in res[:10]:
            c = r["cell"]
            li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
            oos_ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
            lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
            print(f"  {'PASS' if oos_ok else 'fail'}{' LURE' if lure else '    '}"
                  f" IS n={li.get('n', 0):>4} PF={li.get('pf', 0):>6.3f} | "
                  f"OOS n={lo.get('n', 0):>4} PF={lo.get('pf', 0):>6.3f} | "
                  f"ALL n={lv.get('n', 0):>4} PF={lv.get('pf', 0):>6.3f} "
                  f"WR={lv.get('wr_pct', 0):>5} ${lv.get('total_usd', 0):>8.0f} "
                  f"DD=${lv.get('max_dd', 0):>7.0f}  {c}")
        all_res += res
        out_path.write_text(json.dumps(all_res, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
