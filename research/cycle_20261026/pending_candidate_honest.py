"""HONEST-FILL AUDIT of the PENDING CANDIDATE QUEUE (2026-10-26).

WHY
---
Findings #9126 / #9133 / #9139 / #9145 established that the house backtester
(`run_custom_backtest_on_bars`) FILLS AT THE PRICE THE STRATEGY NAMES.  Every
legacy family names a STRUCTURAL price that belongs to an already-closed bar
(the FVG midpoint, the 40% retrace inside the displacement bar, ...).  The
move from that named price to the decision-bar close is never risked, so the
stamped PF is inflated by exactly that gap.

Five candidates are sitting in the queue right now with PF 1.37 - 2.38:
    #7378 MNQ ifvg_midpoint_reversion_htf_bias_5m_nogate   PF 2.377
    #7379 MGC ifvg_midpoint_reversion_htf_bias_5m_nogate   PF 2.152
    #7380 MGC judas_continuation_5m                         PF 2.036
    #7381 MNQ judas_continuation_5m                         PF 1.760
    #7382 MGC sweep_cont_market_entry_15m                   PF 1.374
#7382 is already market-entry (entry = decision close) so it has no retro-fill
artefact by construction.  The other four are NOT.

This script re-runs each of those EXACT codes on the SAME window as the
candidate's own stamp and prints the three books side by side:

    base : fill at the named structural price   (what the house stamp measures)
    fair : fill at the decision bar's close     (market-on-close)
    live : fill at the NEXT bar's open, brackets re-anchored, gap-aware exits
           (the only fill the broker can give -- the live path submits a
            MARKET order, finding #9134: there is no LIMIT entry path)

`live` is what a promoted row would actually earn.  The gap (base - live) is
the retro-fill artefact, measured in PF points AND in dollars.

Read-only w.r.t. the registry and the account.
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

from honest_screen import screen  # noqa: E402

DB = ROOT / "judas_crew.db"

# (label, csid, symbol, tf, days, params)
JOBS = [
    ("#7378 MNQ ifvg_htf_bias_5m_nogate", 282, "MNQ", "5m", 45, {
        "fvg_expiry": 20, "htf_ema_period": 60, "lookback": 20,
        "min_gap_factor": 0.20, "rr": 2.0, "zone_buffer": 0.15}),
    ("#7379 MGC ifvg_htf_bias_5m_nogate", 282, "MGC", "5m", 45, {
        "fvg_expiry": 20, "htf_ema_period": 60, "lookback": 20,
        "min_gap_factor": 0.20, "rr": 2.0, "zone_buffer": 0.15}),
    ("#7380 MGC judas_continuation_5m", 273, "MGC", "5m", 45, {
        "atr_period": 14, "body_ratio_min": 0.70, "disp_atr_mult": 1.3,
        "pullback_pct": 0.40, "sweep_lookback": 5, "target_r": 1.5,
        "stop_buffer_atr": 0.10}),
    ("#7381 MNQ judas_continuation_5m", 273, "MNQ", "5m", 45, {
        "atr_period": 14, "body_ratio_min": 0.70, "disp_atr_mult": 1.3,
        "pullback_pct": 0.40, "sweep_lookback": 5, "target_r": 1.5,
        "stop_buffer_atr": 0.10}),
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
    try:
        r = screen(_code(csid), symbol, tf, params, days=days)
    except Exception as exc:  # noqa: BLE001
        return {"label": label, "csid": csid, "symbol": symbol, "tf": tf,
                "error": f"{type(exc).__name__}: {exc}"}
    r["label"] = label
    r["csid"] = csid
    r["wall_s"] = round(time.time() - t0, 1)
    return r


def main() -> int:
    out = ROOT / "outputs" / "pending_candidate_honest_20261026.json"
    rows = []
    with ProcessPoolExecutor(max_workers=5) as ex:
        for r in ex.map(_job, JOBS):
            rows.append(r)
            if r.get("error"):
                print(f"{r['label']:<52s} ERROR {r['error']}", flush=True)
                continue
            b, lv, fa = r["base"], r["live"], r["fair"]
            m = r.get("meta", {})
            print(f"\n{r['label']}  [{r['symbol']} {r['tf']} {r.get('days')}d]  "
                  f"({r.get('wall_s')}s)", flush=True)
            for nm, bk in (("base(named)", b), ("fair(close)", fa),
                           ("live(next-open)", lv)):
                print(f"   {nm:<16s} n={bk['n']:5d} pf={bk['pf']:6.3f} "
                      f"WR={bk['wr_pct']:5.1f}% E[R]={bk['e_r']:+.3f} "
                      f"${bk['total_usd']:10.0f} dd=${bk['max_dd']:9.0f} "
                      f"gapstops={bk['n_stop_gap']}", flush=True)
            print(f"   entry_offset(median, x bar range) = "
                  f"{m.get('entry_offset_x_bar_range_median')}   "
                  f"pct_entry_outside_decision_bar = "
                  f"{m.get('pct_entry_outside_decision_bar')}", flush=True)
            print(f"   ARTEFACT base_pf {b['pf']:.3f} -> live_pf {lv['pf']:.3f} "
                  f"(delta {b['pf'] - lv['pf']:+.3f}) | "
                  f"$ {b['total_usd']:.0f} -> ${lv['total_usd']:.0f}", flush=True)

    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
