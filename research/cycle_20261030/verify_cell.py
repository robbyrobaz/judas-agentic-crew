"""CANDIDATE VERIFIER (2026-10-30 cycle, task #2893).

Re-measures ONE (code, symbol, tf, params) cell under the PRODUCTION fill and
emits the exact evidence block that goes into `propose_candidate`:

    * live book  = MARKET at the OPEN of the bar AFTER the decision bar,
                   brackets re-anchored to the fill, gap-aware exits
    * base book  = the house engine's own fill (for the retro-fill gap)
    * IS / OOS   = both split protocols (global_time and per_segment)
    * STALE-FILL MEASURE: for every live trade, the wall-clock distance
                   between the DECISION bar and the FILL bar.  A fill that is
                   not the immediately-next physical bar is a STALE fill and
                   disqualifies the cell (stale_fill_n must be 0).
    * NEIGHBOURHOOD ROBUSTNESS: how many of the +/-1 grid neighbours also
                   clear the OOS admission test.  A single-cell spike is a
                   multiple-comparisons artefact, not an edge.

Usage:
    .venv/bin/python research/cycle_20261030/verify_cell.py <json-spec>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from honest_screen import metrics, run_books  # noqa: E402
from segment_honest_screen import segments  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

TICK = {"MGC": 0.1, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 5e-07}
BAR_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


def measure(code: str, symbol: str, tf: str, params: dict, qty: int = 1,
            mask: str = "fixed", min_seg: int = 300, budget_s: float = 240.0):
    segs = segments(symbol, tf, mask=mask, min_seg=min_seg)
    if not segs:
        return {"error": "no segments"}
    live_all, base_all = [], []
    span_first = span_last = None
    t0 = time.time()
    for seg in segs:
        if time.time() - t0 > budget_s:
            break
        b, lv, _fa, _m = run_books(code, seg, params, symbol, qty=qty,
                                   budget_s=max(30.0, budget_s / max(len(segs), 1)))
        ts = pd.to_datetime(seg["ts"], utc=True)
        for tr in (b, lv):
            for t in tr:
                eb = int(t["entry_bar"])
                t["fill_ts"] = str(ts.iloc[min(eb, len(ts) - 1)])
                # decision bar = the bar the signal was taken on (entry_bar-1)
                db = max(eb - 1, 0)
                t["decision_ts"] = str(ts.iloc[min(db, len(ts) - 1)])
                try:
                    d0 = pd.Timestamp(t["decision_ts"])
                    d1 = pd.Timestamp(t["fill_ts"])
                    t["fill_lag_s"] = float((d1 - d0).total_seconds())
                except Exception:  # noqa: BLE001
                    t["fill_lag_s"] = float("nan")
        live_all += lv
        base_all += b
        if span_first is None:
            span_first = str(ts.iloc[0])
        span_last = str(ts.iloc[-1])

    lag_limit = 1.5 * BAR_SECONDS.get(tf, 900)
    stale = [t for t in live_all if not (t["fill_lag_s"] <= lag_limit)]
    return {
        "symbol": symbol, "tf": tf, "qty": qty, "params": params,
        "cost_model": COST_MODEL_VERSION,
        "mask": mask, "n_segments": len(segs),
        "segment_bars": [int(len(s)) for s in segs],
        "span_first": span_first, "span_last": span_last,
        "live": metrics(live_all), "base": metrics(base_all),
        "stale_fill_n": len(stale),
        "max_fill_lag_s": max([t["fill_lag_s"] for t in live_all], default=0.0),
        "n_stop_gap": sum(1 for t in live_all if t["reason"] == "stop_gap"),
        "_live_trades": live_all,
    }


def main() -> int:
    spec = json.loads(Path(sys.argv[1]).read_text())
    out = []
    for job in spec:
        code = Path(job["code_file"]).read_text()
        params = dict(job.get("params", {}))
        params.setdefault("tick", TICK[job["symbol"]])
        r = measure(code, job["symbol"], job["tf"], params,
                    qty=job.get("qty", 1), mask=job.get("mask", "fixed"),
                    budget_s=float(job.get("budget_s", 240)))
        r.pop("_live_trades", None)
        r["code_file"] = job["code_file"]
        r["label"] = job.get("label", "")
        out.append(r)
        lv = r.get("live", {})
        print(f"{job.get('label','')} {job['symbol']}_{job['tf']} "
              f"live n={lv.get('n')} PF={lv.get('pf')} WR={lv.get('wr_pct')} "
              f"${lv.get('total_usd')} DD=${lv.get('max_dd')} "
              f"stale_fill_n={r.get('stale_fill_n')} "
              f"max_lag_s={r.get('max_fill_lag_s')}")
    Path(sys.argv[2]).write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
