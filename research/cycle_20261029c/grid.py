"""HONEST-FILL PARAM GRID on the CORRECTED archive segments (2026-10-29c).

Protocol (identical to the accepted house method, findings #9146 / candidate
#7383 / #7382):

  * FILL      = `live` book: MARKET at the OPEN of the bar AFTER the decision
                bar, brackets re-anchored, gap-aware exits  (production
                semantics -- the only fill the broker can give).
  * COSTS     = src.tools.cost_model.apply_costs_to_trade, COST_MODEL_VERSION
                = "v1_realistic_micros", DOLLAR-denominated, qty as intended.
  * DATA      = per CLEAN SEGMENT (placeholder rows dropped) so a trade can
                never jump a time gap.
  * IS / OOS  = TIME split over the global clean span, first 60% = IS.
                Cells are RANKED ON IS ONLY; OOS is the ADMISSION test
                (n >= 20 OOS trades AND OOS live PF > 1.15).
  * BT-LURE   = flagged when PF > 3.0 AND WR > 70% (the shape findings
                07f3a4c2 / 2311a131 showed collapses to PF 0.70-0.90 under an
                executable fill).

Usage:
    .venv/bin/python research/cycle_20261029c/grid.py <spec.json> <out.json>
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
DOLLARS_PER_POINT = {"MGC": 10.0, "MNQ": 2.0, "MCL": 10.0, "ZF": 1000.0,
                      "6J": 12500000.0}

BASE = {"lookback": 20, "min_sweep_atr": 0.6, "disp_k": 1.0, "rr": 2.0,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}


def grid_cells():
    cells = []
    for lb, ms, dk, rr in itertools.product(
            (8, 12, 20), (0.30, 0.45, 0.60), (0.8, 1.0, 1.2), (1.0, 1.5, 2.0)):
        c = dict(BASE)
        c.update({"lookback": lb, "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
        cells.append(c)
    return cells


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
    return r


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    all_res = []
    for job in spec:
        cells = job.get("cells") or grid_cells()
        jobs = [(job, c) for c in cells]
        res = []
        with ProcessPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
            for r in ex.map(_one, jobs):
                res.append(r)
        # rank on IS ONLY
        res.sort(key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                -(r["live_IS"].get("n") or 0)))
        print(f"\n### {job['label']} {job['symbol']}_{job['tf']}  "
              f"({len(res)} cells, ranked on IS)")
        print(f"{'lb':>3}{'ms':>6}{'dk':>5}{'rr':>5} | "
              f"{'IS n':>5}{'IS PF':>7}{'IS $':>10} | "
              f"{'OOS n':>6}{'OOS PF':>7}{'OOS $':>10}{'OOS WR':>8} | "
              f"{'ALL PF':>8}{'WR':>6} verdict")
        for r in res[:12]:
            c = r["cell"]
            li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
            oos_ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
            lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
            verdict = "PASS" if oos_ok else "fail"
            if lure:
                verdict += " BT-LURE?"
            print(f"{c['lookback']:>3}{c['min_sweep_atr']:>6}{c['disp_k']:>5}"
                  f"{c['rr']:>5} | {li.get('n',0):>5}{li.get('pf',0):>7.3f}"
                  f"{li.get('total_usd',0):>10.0f} | {lo.get('n',0):>6}"
                  f"{lo.get('pf',0):>7.3f}{lo.get('total_usd',0):>10.0f}"
                  f"{lo.get('wr_pct',0):>8} | {lv.get('pf',0):>8.3f}"
                  f"{lv.get('wr_pct',0):>6} {verdict}")
        all_res += res
    out_path.write_text(json.dumps(all_res, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
