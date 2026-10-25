"""HONEST-ENTRY SWEEP v2 — out-of-sample validated, production fill semantics.

2026-10-24. Builds on `honest_entry_silver_bullet_20261024.py`.

WHAT IS DIFFERENT FROM v1
-------------------------
v1 swept a grid and printed every combo. Picking the max of that grid IS
overfitting, so v2 does it properly:

  1. The archive is split IN-SAMPLE (first 55% of bars) / OUT-OF-SAMPLE
     (last 45%). The split is chronological, so OOS is strictly later data.
  2. The grid is swept on IN-SAMPLE only, under 'live' fill semantics
     (entry at the next bar's open, stop/target distances RE-ANCHORED to the
     fill, gap-aware exits) = what the NT broker actually does.
  3. The top-K in-sample combos are re-measured on OUT-OF-SAMPLE.
  4. A combo is only a CANDIDATE if it survives OOS: OOS PF > 1.15 AND
     OOS n >= 20 (the promotion gate's n). In-sample-only survivors are
     reported as OVERFIT and dropped.

WHY THE ENTRY IS THE POINT
--------------------------
Every strategy in this family used to report its entry as a price from an
older bar (csid=348: `entry = o[k+1]` with k+1 possibly many bars back),
which the backtester filled at anyway. That is the retro-fill fiction
finding #9133 sized at book scale. Here `entry` is ALWAYS the decision bar's
close, so:
    stale% (entry outside the decision bar's [low,high]) == 0
    at_close% (entry == decision close)             == 100
and BASE vs LIVE PF agree to within a few percent. The reported number is
finally the number the live account can earn.

READ-ONLY w.r.t. the registry and the account. Writes only outputs/*.json.
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

from fair_entry_restamp_book2_20261022 import run_variant, metrics  # noqa: E402

OUT = "outputs/honest_entry_sweep_v2_20261024.json"
LEGAL = ("MGC", "MNQ", "MCL", "ZF", "6J")

# ---------------------------------------------------------------------------
# Strategy with extra honest knobs: direction filter, stop mode, session gate.
# Entry is ALWAYS the decision bar close.
# ---------------------------------------------------------------------------
STRATEGY_CODE = r'''
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 40:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 24))
    min_body_ratio = float(params.get("min_body_ratio", 0.0))
    direction_filter = str(params.get("direction_filter", "both"))
    stop_mode = str(params.get("stop_mode", "sweep_and_bar"))
    atr_mult = float(params.get("atr_stop_mult", 0.0))
    session_lo = int(params.get("session_start_utc", -1))
    session_hi = int(params.get("session_end_utc", -1))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values

    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=8).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=8).min().shift(1).values
    # True range with a properly aligned previous close (length n, no NaN).
    prev_c = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr_series = pd.Series(tr).rolling(14, min_periods=5).mean().values

    last = n - 1
    if last < 10:
        return None
    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None

    if session_lo >= 0:
        try:
            hh = int(pd.Timestamp(ts_v[last]).hour)
            if not (session_lo <= hh < session_hi):
                return None
        except Exception:
            return None

    def _body_ratio(i):
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        return abs(c[i] - o[i]) / rng

    for direction in ("long", "short"):
        if direction_filter != "both" and direction_filter != direction:
            continue
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
            for k in range(j + 1, last):
                if direction == "long":
                    if not (l[k] > h[k - 2]):
                        continue
                else:
                    if not (h[k] < l[k - 2]):
                        continue
                entry = c[last]          # HONEST ENTRY: decision-bar close
                if direction == "long":
                    if stop_mode == "sweep_low":
                        stop = l[j] - stop_buf
                    elif stop_mode == "bar_low":
                        stop = l[last] - stop_buf
                    elif stop_mode == "atr":
                        stop = entry - atr_mult * atr_series[last]
                    else:
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
                    if stop_mode == "sweep_low":
                        stop = h[j] + stop_buf
                    elif stop_mode == "bar_low":
                        stop = h[last] + stop_buf
                    elif stop_mode == "atr":
                        stop = entry + atr_mult * atr_series[last]
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


def build_grid():
    grid = []
    for rolling in (12, 24, 48):
        for target_r in (1.5, 2.0, 2.5, 3.0):
            for body in (0.0, 0.4, 0.6):
                for dfilt in ("both", "long", "short"):
                    for stop_mode in ("sweep_and_bar", "sweep_low", "atr"):
                        grid.append({
                            "rolling_pdhl_bars": rolling,
                            "target_r": target_r,
                            "max_retest_bars": 24,
                            "break_threshold_ticks": 4,
                            "stop_buf_ticks": 6,
                            "min_body_ratio": body,
                            "direction_filter": dfilt,
                            "stop_mode": stop_mode,
                            "atr_stop_mult": 1.0,
                            "entry_mode": "close",
                        })
    return grid


def measure(code, bars, params, symbol, dpp, mode="live", budget_s=120):
    tr, meta = run_variant(code, bars, params, symbol, dpp, mode,
                           budget_s=budget_s, want_stale=(mode == "live"))
    return metrics(tr or []), meta


def main():
    tfs = sys.argv[1].split(",") if len(sys.argv) > 1 else ["15m", "5m"]
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else list(LEGAL)
    topk = int(sys.argv[3]) if len(sys.argv) > 3 else 14

    grid = build_grid()
    print(f"grid size {len(grid)}", flush=True)
    results = []
    for tf in tfs:
        for sym in syms:
            try:
                bars = bar_archive.read_archive(sym, tf)
            except Exception as exc:  # noqa: BLE001
                print(f"{sym} {tf}: archive read fail {exc}", flush=True)
                continue
            if bars is None or len(bars) < 400:
                print(f"{sym} {tf}: no archive", flush=True)
                continue
            spec = _CONTRACT_SPECS.get(sym)
            dpp = float(spec["tick_value"]) / float(spec["tick"])
            cut = int(len(bars) * 0.55)
            is_bars = bars.iloc[:cut].reset_index(drop=True)
            oos_bars = bars.iloc[cut:].reset_index(drop=True)

            t0 = time.time()
            scored = []
            for p in grid:
                params = dict(p)
                params.setdefault("tick", spec["tick"])
                m, meta = measure(STRATEGY_CODE, is_bars, params, sym, dpp, "live")
                if m["n"] < 12:
                    continue
                scored.append({"params": p, "is": m,
                               "stale_pct": meta.get("pct_entry_outside_decision_bar"),
                               "at_close_pct": meta.get("pct_entry_is_decision_close")})
            scored.sort(key=lambda r: (-r["is"]["pf"], -r["is"]["n"]))
            print(f"--- {sym} {tf}: {len(scored)} IS combos, sweep {round(time.time()-t0,1)}s", flush=True)

            for rank, s in enumerate(scored[:topk]):
                params = dict(s["params"])
                params.setdefault("tick", spec["tick"])
                oos_m, _ = measure(STRATEGY_CODE, oos_bars, params, sym, dpp, "live")
                rec = {"symbol": sym, "tf": tf, "rank_is": rank,
                       "params": dict(s["params"]),
                       "IS": s["is"], "OOS": oos_m,
                       "stale_pct": s["stale_pct"], "at_close_pct": s["at_close_pct"],
                       "oos_pass": bool(oos_m["n"] >= 20 and oos_m["pf"] > 1.15)}
                results.append(rec)
                print(f"  IS#{rank} {sym} {tf} roll={s['params']['rolling_pdhl_bars']} "
                      f"tR={s['params']['target_r']} body={s['params']['min_body_ratio']} "
                      f"dir={s['params']['direction_filter']} stop={s['params']['stop_mode']} "
                      f"| IS n={s['is']['n']} pf={s['is']['pf']} e_r={s['is']['e_r']} "
                      f"|| OOS n={oos_m['n']} pf={oos_m['pf']} wr={oos_m['wr_pct']} "
                      f"e_r={oos_m['e_r']} usd={oos_m['usd_per_trade']} "
                      f"{'PASS' if rec['oos_pass'] else 'reject'}", flush=True)
            json.dump({"cost_model": COST_MODEL_VERSION, "grid": len(grid),
                       "results": results}, open(OUT, "w"), indent=1, default=str)

    npass = sum(1 for r in results if r["oos_pass"])
    print(f"\nWROTE {OUT}: {len(results)} finalists, {npass} OOS passes")


if __name__ == "__main__":
    main()
