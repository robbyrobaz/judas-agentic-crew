"""FINAL VALIDATION for a candidate cell (task #2893).

Re-runs one configuration under the PRODUCTION fill and emits every number
the registrar's four gates need, plus the three anti-lure checks the
operator demanded:

  GATE 1  PF >= 1.3
  GATE 2  n >= 20 trades
  GATE 3  E[R] = (WR x avg_win) - ((1-WR) x avg_loss) > 0
  GATE 4  dollar-denominated, cost_model = v1_realistic_micros, qty as intended

  BT-LURE   PF > 3.0 AND WR > 70%  ->  the shape findings 07f3a4c2 / 2311a131
            showed collapsing to PF 0.70-0.90 under an executable fill.
  STALE     stale_fill_n = 0 -- no entry is ever resolved at a price the
            decision could already see.  The live book fills at the OPEN of
            the physical bar AFTER the decision bar, so this is structural;
            this script MEASURES it rather than asserting it.
  IS/OOS    reported under BOTH split protocols ("global_time" and
            "per_segment").  A cell that survives only one is not a cell.

The script also measures DOLLAR THROUGHPUT (usd_per_trade x trades/week),
which is the operator's requested ranking key, and the exact time hole
between the decision bar and the entry bar.

Usage:
    .venv/bin/python research/cycle_20261029d/validate.py <cells.json> <out.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from honest_screen import metrics, run_books  # noqa: E402
from segment_honest_screen import segments  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

BAR_SECONDS = {"1h": 3600, "15m": 900, "5m": 300}
IS_FRAC = 0.60


def validate(code: str, symbol: str, tf: str, params: dict, qty: int = 1,
             mask: str = "fixed", min_seg: int = 300, budget_s: float = 300.0):
    sym = str(symbol).upper()
    segs = segments(sym, tf, mask=mask, min_seg=min_seg)
    if not segs:
        return {"error": "no segments", "symbol": sym, "tf": tf}

    live_all, base_all, fair_all = [], [], []
    per_seg_bounds = [int(len(s) * IS_FRAC) for s in segs]
    stale_fill_n = 0
    hole_n = 0
    max_gap_s = 0
    span_first, span_last = None, None

    for si, seg in enumerate(segs):
        b, lv, fa, _ = run_books(code, seg, params, sym, qty=qty,
                                 budget_s=max(30.0, budget_s / max(len(segs), 1)))
        ts = pd.to_datetime(seg["ts"], utc=True)
        for tr in (b, lv, fa):
            for t in tr:
                eb = int(t["entry_bar"])
                t["seg"] = si
                t["bar_idx"] = eb
                t["ts"] = str(ts.iloc[min(eb, len(seg) - 1)])
                if tr is lv:
                    # the decision bar is entry_bar-1; the entry price is the
                    # OPEN of entry_bar.  If entry_bar <= decision_bar the
                    # fill is retroactive -> stale.
                    if eb <= 0:
                        stale_fill_n += 1
                    else:
                        gap_s = float((ts.iloc[min(eb, len(seg) - 1)]
                                       - ts.iloc[max(eb - 1, 0)]).total_seconds())
                        max_gap_s = max(max_gap_s, gap_s)
                        if gap_s > 1.5 * BAR_SECONDS.get(tf, 300):
                            hole_n += 1
        for tr, sink in ((lv, live_all), (b, base_all), (fa, fair_all)):
            sink += tr
        if span_first is None:
            span_first = ts.iloc[0]
        span_last = ts.iloc[-1]

    t0 = pd.Timestamp(span_first)
    t1 = pd.Timestamp(span_last)
    span_days = max((t1 - t0).total_seconds() / 86400.0, 1e-9)
    boundary = t0 + (t1 - t0) * IS_FRAC

    def split(trades):
        gi = [t for t in trades
              if t.get("bar_idx", 0) <= per_seg_bounds[t["seg"]]]
        go = [t for t in trades
              if t.get("bar_idx", 0) > per_seg_bounds[t["seg"]]]
        # the global-time protocol
        gx = [t for t in trades if pd.Timestamp(t["ts"]) <= boundary]
        gy = [t for t in trades if pd.Timestamp(t["ts"]) > boundary]
        return gi, go, gx, gy

    li, lo, gi, go = split(live_all)
    lv_m = metrics(live_all)
    base_m = metrics(base_all)
    fair_m = metrics(fair_all)

    n = lv_m.get("n") or 0
    trades_per_week = (n / span_days) * 7.0 if span_days else 0.0
    usd_per_trade = lv_m.get("usd_per_trade") or 0.0
    pf = lv_m.get("pf") or 0.0
    wr = lv_m.get("wr_pct") or 0.0
    e_r = lv_m.get("e_r") or 0.0

    oos_seg = metrics(lo)
    oos_glob = metrics(go)
    is_seg = metrics(li)
    is_glob = metrics(gi)

    lure = pf > 3.0 and wr > 70.0
    gates = {
        "g1_pf_ge_1.3": pf >= 1.3,
        "g2_n_ge_20": n >= 20,
        "g3_expectancy_r_gt_0": e_r > 0,
        "g4_dollar_cost_model": COST_MODEL_VERSION == "v1_realistic_micros",
        "oos_seg_admission": (oos_seg.get("n") or 0) >= 20
        and (oos_seg.get("pf") or 0) > 1.15,
        "oos_glob_admission": (oos_glob.get("n") or 0) >= 20
        and (oos_glob.get("pf") or 0) > 1.15,
        "not_bt_lure": not lure,
        "stale_fill_n_zero": stale_fill_n == 0,
    }

    return {
        "symbol": sym, "tf": tf, "qty": qty, "params": params,
        "cost_model": COST_MODEL_VERSION,
        "n_segments": len(segs),
        "seg_bars": [int(len(s)) for s in segs],
        "span_first": str(t0), "span_last": str(t1), "span_days": round(span_days, 1),
        "live": lv_m,
        "live_IS_perseg": is_seg, "live_OOS_perseg": oos_seg,
        "live_IS_global": is_glob, "live_OOS_global": oos_glob,
        "base_house": base_m, "fair_moc": fair_m,
        "stale_fill_n": stale_fill_n,
        "time_hole_n": hole_n,
        "max_decision_to_entry_gap_s": max_gap_s,
        "bt_lure": lure,
        "trades_per_week": round(trades_per_week, 2),
        "usd_per_week": round(usd_per_trade * trades_per_week, 2),
        "gates": gates,
        "all_gates_pass": all(gates.values()),
        "_live_trades": live_all,
    }


if __name__ == "__main__":
    cells = json.loads(Path(sys.argv[1]).read_text())
    out = []
    for c in cells:
        code = Path(c["code_file"]).read_text()
        r = validate(code, c["symbol"], c["tf"], c["params"],
                     qty=c.get("qty", 1), mask=c.get("mask", "fixed"))
        r.pop("_live_trades", None)
        r["label"] = c.get("label", "")
        r["code_file"] = c["code_file"]
        out.append(r)
        g = r.get("gates", {})
        print(f"\n### {r.get('label')} {r.get('symbol')}_{r.get('tf')}")
        if r.get("error"):
            print("  ERROR", r["error"])
            continue
        lv = r["live"]
        print(f"  LIVE  n={lv['n']} PF={lv['pf']} WR={lv['wr_pct']}% "
              f"E[R]={lv['e_r']} ${lv['total_usd']} DD=${lv['max_dd']} "
              f"usd/trade=${lv['usd_per_trade']} fires/wk={r['trades_per_week']} "
              f"$/wk={r['usd_per_week']}")
        for tag, m in (("IS/perseg", r["live_IS_perseg"]), ("OOS/perseg", r["live_OOS_perseg"]),
                       ("IS/global", r["live_IS_global"]), ("OOS/global", r["live_OOS_global"])):
            print(f"  {tag:11s} n={m['n']:>4} PF={m['pf']:>6.3f} "
                  f"WR={m['wr_pct']:>5}% E[R]={m['e_r']:>6} ${m['total_usd']:>9.2f}")
        print(f"  stale_fill_n={r['stale_fill_n']} time_hole_n={r['time_hole_n']} "
              f"max_gap={r['max_decision_to_entry_gap_s']}s bt_lure={r['bt_lure']}")
        print(f"  GATES: " + "  ".join(f"{k}={'Y' if v else 'N'}" for k, v in g.items())
              + f"   ==> {'ALL PASS' if r['all_gates_pass'] else 'FAIL'}")
    Path(sys.argv[2]).write_text(json.dumps(out, indent=2, default=str))
