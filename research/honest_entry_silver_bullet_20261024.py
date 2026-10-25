"""HONEST-ENTRY SILVER BULLET (PDH/PDL sweep + continuation) — 2026-10-24.

WHY THIS EXISTS
---------------
The house `silver_bullet_pdh_pdl_retest_*` family reports PF 3-6 in the
production backtester, but finding #9133 (shadow ledger) and the #2869 v3
restamp show that number is an ARTEFACT of the entry price, not of the
setup.

Measured on csid=348 (MGC 5m, active #4857):

    BASE  (fill at the signal's own entry)   n=1314  PF=6.474  WR=84.6%
    FAIR  (fill at the decision bar close)   n=1106  PF=0.818  WR=32.2%
    LIVE  (fill at next bar open, re-anchored) n=1095 PF=0.853  WR=33.4%
    -> 85.7% of its entry prices lie OUTSIDE the decision bar's [low, high].

Root cause, read off the csid=348 source: it sets

    entry = o[entry_idx]      with entry_idx = k + 1,  k < last

i.e. the entry is the OPEN OF A BAR THAT MAY BE SEVERAL BARS OLD, while the
trigger is the CURRENT bar's high breaking the rolling PDH. The entry price
therefore precedes the event that produced the signal — a price the market has
already left behind. The backtester fills there anyway. That is the fiction.

THE FIX
-------
Enter at the CLOSE OF THE DECISION BAR. That price was traded by the market at
exactly the moment the decision was made, so:

  * BASE fill  = c[i]      (decision bar close)
  * LIVE fill  = o[i+1]    (next bar open, production semantics)
  * FAIR fill  = c[i]

and the three agree to within one bar of drift. The reported PF becomes an
HONEST number, and the backtest no longer rewards an impossible fill.

Everything else about the setup is preserved from csid=348 (rolling PDH/PDL
sweep, retrace bar, LTF structure shift, stop under the sweep low) so the
comparison is apples-to-apples: same setup, honest price.

THE SWEEP
---------
Measures every param combo under BOTH fill modes:
  * 'live' = production semantics (entry at next bar open, stop/target
    distances RE-ANCHORED to the fill, gap-aware exits) — the verdict number.
  * 'base' = what run_custom_backtest would report — the honesty check.
A combo is only interesting if live PF is good AND base PF is close to live
PF. If base >> live the combo still has fill fiction and is rejected.

READ-ONLY w.r.t. the registry and the account. Writes only outputs/*.json.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

from fair_entry_restamp_book2_20261022 import run_variant, metrics  # noqa: E402

DB = "judas_crew.db"
OUT = "outputs/honest_entry_sb_20261024.json"

LEGAL = ("MGC", "MNQ", "MCL", "ZF", "6J")

# ---------------------------------------------------------------------------
# The strategy.  Entry == decision-bar close.  Everything else mirrors
# csid=348 so the only changed variable is the fill.
# ---------------------------------------------------------------------------
STRATEGY_CODE = r'''
import numpy as np
import pandas as pd


def evaluate(bars, params):
    """Rolling PDH/PDL sweep + LTF structure shift, entered at the DECISION BAR CLOSE.

    HONEST ENTRY: `entry` is the close of the last bar handed to us. That is
    a price the market traded at the instant of the decision, so the
    backtester's fill (and the live market order's fill) is achievable.
    """
    if bars is None or len(bars) < 40:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 24))
    min_body_ratio = float(params.get("min_body_ratio", 0.0))
    require_sweep_wick = int(params.get("require_sweep_wick", 0))
    entry_mode = str(params.get("entry_mode", "close"))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values

    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=8).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=8).min().shift(1).values

    last = n - 1
    if last < 10:
        return None
    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None

    def _body_ratio(i):
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        return abs(c[i] - o[i]) / rng

    for direction in ("long", "short"):
        if direction == "long":
            if not (h[last] >= pdh_series[last] + break_thr):
                continue
        else:
            if not (l[last] <= pdl_series[last] - break_thr):
                continue
        if min_body_ratio > 0 and _body_ratio(last) < min_body_ratio:
            continue
        for j in range(max(0, last - max_retest), last):
            if direction == "long":
                if not (c[j] < pdh_series[j] and l[j] >= pdl_series[j]):
                    continue
            else:
                if not (c[j] > pdl_series[j] and h[j] <= pdh_series[j]):
                    continue
            if require_sweep_wick:
                if direction == "long" and not (h[j] > c[j]):
                    continue
                if direction == "short" and not (l[j] < c[j]):
                    continue
            for k in range(j + 1, last):
                if direction == "long":
                    if not (l[k] > h[k - 2]):
                        continue
                else:
                    if not (h[k] < l[k - 2]):
                        continue
                # --- HONEST ENTRY -------------------------------------
                if entry_mode == "close":
                    entry = c[last]
                elif entry_mode == "open_decision":
                    entry = o[last]
                else:
                    entry = c[last]
                # ----------------------------------------------------
                if direction == "long":
                    stop = min(l[j], l[last]) - stop_buf
                    risk = entry - stop
                    if risk < 0.5 * tick:
                        continue
                    target = entry + risk * target_r
                    if target <= entry:
                        continue
                    return {"ts": ts_v[last], "direction": "long",
                            "entry": float(entry), "stop": float(stop),
                            "target": float(target)}
                else:
                    stop = max(h[j], h[last]) + stop_buf
                    risk = stop - entry
                    if risk < 0.5 * tick:
                        continue
                    target = entry - risk * target_r
                    if target >= entry:
                        continue
                    return {"ts": ts_v[last], "direction": "short",
                            "entry": float(entry), "stop": float(stop),
                            "target": float(target)}
            break
    return None
'''


def _sweep(symbol, tf, param_grid):
    """Run every param combo on one (symbol, tf) under live + base."""
    bars = bar_archive.read_archive(symbol, tf)
    if bars is None or len(bars) == 0:
        return []
    spec = _CONTRACT_SPECS.get(symbol)
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    out = []
    for p in param_grid:
        params = dict(p)
        params.setdefault("tick", spec["tick"])
        t0 = time.time()
        rec = {"symbol": symbol, "tf": tf, "params": dict(p),
               "cost_model": COST_MODEL_VERSION}
        for mode in ("live", "base"):
            tr, meta = run_variant(STRATEGY_CODE, bars, params, symbol, dpp,
                                   mode, budget_s=180,
                                   want_stale=(mode == "live"))
            m = metrics(tr or [])
            rec[mode.upper()] = m
            if mode == "live":
                rec["stale_pct"] = meta.get("pct_entry_outside_decision_bar")
                rec["at_close_pct"] = meta.get("pct_entry_is_decision_close")
        lv, bs = rec["LIVE"], rec["BASE"]
        rec["live_gap"] = round(bs["pf"] - lv["pf"], 3)
        rec["honest"] = bool(lv["pf"] > 0 and bs["pf"] <= lv["pf"] * 1.25)
        rec["wall_s"] = round(time.time() - t0, 1)
        out.append(rec)
    return out


def main():
    # ---- param grid -----------------------------------------------------
    grid = []
    for rolling in (12, 24, 48):
        for target_r in (1.5, 2.0, 3.0):
            for body in (0.0, 0.5):
                grid.append({
                    "rolling_pdhl_bars": rolling,
                    "target_r": target_r,
                    "max_retest_bars": 24,
                    "break_threshold_ticks": 4,
                    "stop_buf_ticks": 6,
                    "min_body_ratio": body,
                    "entry_mode": "close",
                })

    tfs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["15m", "5m"]
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(LEGAL)

    all_rows = []
    for tf in tfs:
        for sym in syms:
            rows = _sweep(sym, tf, grid)
            all_rows += rows
            # progress print
            for r in rows:
                print(f"[{sym} {tf}] roll={r['params']['rolling_pdhl_bars']} "
                      f"tR={r['params']['target_r']} body={r['params']['min_body_ratio']} "
                      f"| LIVE n={r['LIVE']['n']} pf={r['LIVE']['pf']} wr={r['LIVE']['wr_pct']} "
                      f"e_r={r['LIVE']['e_r']} usd={r['LIVE']['usd_per_trade']} "
                      f"| BASE pf={r['BASE']['pf']} | stale%={r.get('stale_pct')} "
                      f"atclose%={r.get('at_close_pct')}", flush=True)
            json.dump({"cost_model": COST_MODEL_VERSION, "rows": all_rows},
                      open(OUT, "w"), indent=1, default=str)

    print("WROTE", OUT, len(all_rows), "rows")


if __name__ == "__main__":
    main()
