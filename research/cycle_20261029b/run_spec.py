"""Parallel driver for the segment-aware honest screen.

Screens a list of (csid, symbol, timeframe) jobs with the CORRECTED segment
mask and the PRODUCTION ('live') fill, dollar-denominated with
cost_model=v1_realistic_micros, and prints a leaderboard.

Usage:
    .venv/bin/python research/cycle_20261029b/run_spec.py <spec.json> <out.json> [mask]
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


def job(args):
    job, mask = args
    code = Path(job["code_file"]).read_text()
    params = dict(job.get("params") or {})
    params.setdefault("tick", TICK[job["symbol"]])
    r = screen(code, job["symbol"], job["tf"], params,
               qty=job.get("qty", 1), mask=mask,
               budget_s=float(job.get("budget_s", 240)))
    r.pop("_live_trades", None)
    r["csid"] = job.get("csid")
    r["label"] = job.get("label", "")
    return r


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    mask = sys.argv[3] if len(sys.argv) > 3 else "fixed"
    out_path = Path(sys.argv[2])
    jobs = [(j, mask) for j in spec]
    results = []
    with ProcessPoolExecutor(max_workers=min(8, max(1, len(jobs)))) as ex:
        for r in ex.map(job, jobs):
            results.append(r)
            lv = r.get("live", {})
            lo = r.get("live_OOS", {})
            print(f"{r.get('label',''):<34} {r['symbol']}_{r['tf']:<4} "
                  f"seg={r.get('n_segments')} bars={r.get('segment_bars')} "
                  f"| LIVE n={lv.get('n')} PF={lv.get('pf')} ${lv.get('total_usd')} "
                  f"WR={lv.get('wr_pct')}% E[R]={lv.get('e_r')} DD={lv.get('max_dd')} "
                  f"| OOS n={lo.get('n')} PF={lo.get('pf')} ${lo.get('total_usd')}",
                  flush=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
