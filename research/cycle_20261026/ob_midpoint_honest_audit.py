"""HONEST-FILL AUDIT EXTENSION -- the TWO candidates the 2026-10-26 audit MISSED.

WHY
---
Finding 2311a131 ("4 OF 5 PENDING CANDIDATES ARE RETRO-FILL ARTEFACTS")
audited #7378 / #7379 / #7380 / #7381 and used #7382 as the control.  It did
NOT audit #7376 (6J ob_midpoint_reversion_15m) or #7377 (ZF
ob_midpoint_reversion_15m) -- both csid=318, both stamped PF 2.12 / 1.82 on a
BASE (fill-at-the-named-price) book.

That omission matters, because the SAME finding states the mechanism in
general terms:

    "any family whose strategy NAMES a structural entry price (FVG midpoint,
     OB midpoint, sweep extreme, mother_h, 40% retrace) carries an
     unmeasurable retro-fill premium in its stamp"

An OB midpoint IS a named structural price.  So #7376/#7377 are exactly the
class the finding predicts will collapse, and they sit in the queue with the
highest stamped PF of anything pending.  They have to be measured, not
assumed -- in either direction.

THIS FILE
---------
Runs the SAME two harnesses the researcher used, on the SAME windows as the
candidates' own stamps:

    base  = fill at the NAMED structural price (what the house stamp measures)
    fair  = fill at the decision bar's CLOSE
    live  = MARKET at the NEXT bar's OPEN, brackets re-anchored, gap-aware
            exits -- the only fill the broker can give (#9134)
    limit = CHARITABLE: a LIMIT at the named price, filled only if a later bar
            actually trades through it within 10 bars, else the signal expires

#7382 (csid=439, market entry by construction) is re-run as a CONTROL: if my
harness reproduces the published numbers for it, the harness is trustworthy for
the two new rows.

READ-ONLY w.r.t. the registry and the account.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import screen  # noqa: E402
from limit_book import run_limit_book  # noqa: E402
from honest_screen import load_bars, metrics  # noqa: E402

DB = ROOT / "judas_crew.db"

# (label, csid, symbol, tf, days, params)  -- days mirrors the candidate stamp
JOBS = [
    ("#7376 6J ob_midpoint_reversion_15m", 318, "6J", "15m", 90, {
        "atr_period": 14, "body_ratio_min": 0.5, "displacement_k": 1.0,
        "ob_age_max": 24, "rr": 2.0, "stop_buf_atr": 0.20}),
    ("#7377 ZF ob_midpoint_reversion_15m", 318, "ZF", "15m", 90, {
        "atr_period": 14, "body_ratio_min": 0.5, "displacement_k": 1.0,
        "ob_age_max": 24, "rr": 2.0, "stop_buf_atr": 0.20}),
    ("#7382 MGC sweep_cont_market_entry_15m (CONTROL)", 439, "MGC", "15m", 120, {
        "lookback": 12, "min_sweep_atr": 0.30, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}),
]


def _code(csid: int) -> str:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"csid {csid} not found")
    return row[0]


def _job(args):
    label, csid, symbol, tf, days, params = args
    t0 = time.time()
    code = _code(csid)
    out = {"label": label, "csid": csid, "symbol": symbol, "tf": tf, "days": days}
    try:
        r = screen(code, symbol, tf, params, days=days)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"screen {type(exc).__name__}: {exc}"
        return out
    out.update(r)
    try:
        bars = load_bars(symbol, tf, days=days)
        trades, meta = run_limit_book(code, bars, params, symbol)
        lm = metrics(trades)
        lm["fill_rate_pct"] = (round(100.0 * meta["n_filled"] / meta["n_signals"], 1)
                               if meta["n_signals"] else 0.0)
        out["limit"] = lm
        out["limit_meta"] = meta
    except Exception as exc:  # noqa: BLE001
        out["limit_error"] = f"{type(exc).__name__}: {exc}"
    out["wall_s"] = round(time.time() - t0, 1)
    return out


def main() -> int:
    outp = ROOT / "outputs" / "ob_midpoint_honest_20261026.json"
    rows = []
    with ProcessPoolExecutor(max_workers=3) as ex:
        for r in ex.map(_job, JOBS):
            rows.append(r)
            if r.get("error"):
                print(f"{r['label']:<50s} ERROR {r['error']}", flush=True)
                continue
            b, lv, fa = r["base"], r["live"], r["fair"]
            m = r.get("meta", {})
            print(f"\n{r['label']}  [{r['symbol']} {r['tf']} {r.get('days')}d] "
                  f"({r.get('wall_s')}s)", flush=True)
            for nm, bk in (("base(named)", b), ("fair(close)", fa),
                           ("live(next-open)", lv)):
                print(f"   {nm:<16s} n={bk['n']:5d} pf={bk['pf']:6.3f} "
                      f"WR={bk['wr_pct']:5.1f}% E[R]={bk['e_r']:+.3f} "
                      f"${bk['total_usd']:10.0f} dd=${bk['max_dd']:9.0f}", flush=True)
            if r.get("limit"):
                lm = r["limit"]
                print(f"   {'LIMIT(charit)':<16s} n={lm['n']:5d} pf={lm['pf']:6.3f} "
                      f"WR={lm['wr_pct']:5.1f}% E[R]={lm['e_r']:+.3f} "
                      f"${lm['total_usd']:10.0f} fill_rate={lm.get('fill_rate_pct')}%",
                      flush=True)
            if r.get("limit_error"):
                print(f"   LIMIT ERROR {r['limit_error']}", flush=True)
            print(f"   entry_offset(median,x bar range)="
                  f"{m.get('entry_offset_x_bar_range_median')}  "
                  f"pct_entry_outside_decision_bar="
                  f"{m.get('pct_entry_outside_decision_bar')}", flush=True)
            print(f"   ARTEFACT base_pf {b['pf']:.3f} -> live_pf {lv['pf']:.3f} "
                  f"(delta {b['pf'] - lv['pf']:+.3f}) | "
                  f"${b['total_usd']:.0f} -> ${lv['total_usd']:.0f}", flush=True)
    outp.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
