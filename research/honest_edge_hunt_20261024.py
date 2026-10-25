"""HONEST-EDGE HUNT — 2026-10-24.

WHY
---
Finding #9133 (shadow ledger) + the #2869 v3 restamp proved the house
backtest number is a FILL ARTEFACT: the production families set
`entry = o[entry_idx]` with `entry_idx = k + 1` where `k` may be many
bars OLD, so the backtester fills at a price the market had already left.
Restamped under an achievable fill, every remeasured active is DEAD or
UNVERIFIED:

    #4885 MGC 15m ifvg   bt PF 2.07  ->  FAIR 0.826 / LIVE 0.869
    #4938 6J  1h sb     bt PF 3.58  ->  FAIR 0.532 / LIVE 0.497  (7.97x inflation)
    #4931 MGC 5m ob     bt PF 1.88  ->  FAIR 0.871 / LIVE 0.857

So the question this script answers is NOT "which library member has the
highest PF" (all of them lie). It is:

    IS THERE ANY SETUP WHOSE EDGE SURVIVES AN ACHIEVABLE FILL?

WHAT MAKES A FILL "ACHIEVABLE" HERE
------------------------------------
  * the decision is taken on bar i (the last bar handed to evaluate());
  * the order is a MARKET order sent at that instant;
  * therefore the fill is the price the market is offering at that instant:
      FAIR  = close of the decision bar   (c[i])
      LIVE  = open of the next bar        (o[i+1])  <- production semantics
  * stops/targets are RE-ANCHORED to the fill (the distances the strategy
    asked for are preserved, the prices move with the fill);
  * exits are GAP-AWARE: a bar that gaps through the stop fills at the
    open, not at the stop price.

Both modes are computed for every config. A config is only interesting if
BOTH clear PF > 1.0 -- if the two disagree wildly the config is still
carrying fill fiction.

THE HARNESS
------------
`run_fast` mirrors research/fair_entry_restamp_book2_20261022.run_variant
(same modes, same gap logic, same metric definitions) but hands the
strategy a bounded TAIL WINDOW instead of the whole growing frame. Every
template here looks back at most 250 bars, so a 700-bar window is
semantically identical while turning O(n^2) into O(n * w).

READ-ONLY w.r.t. the registry and the account. Writes only outputs/*.json.
"""
from __future__ import annotations

import itertools
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402
from fair_entry_restamp_book2_20261022 import _compile_and_load, metrics  # noqa: E402

OUT = "outputs/honest_edge_hunt_20261024.json"
TAIL = 700

# --------------------------------------------------------------------------
# TEMPLATE 1 — honest silver bullet (rolling PDH/PDL sweep -> retrace ->
# LTF structure shift -> trigger break), entered at the DECISION BAR CLOSE.
# --------------------------------------------------------------------------
CODE_SB = r'''
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None
    rolling_window = int(params.get("rolling_pdhl_bars", 24))
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * float(params.get("tick", 0.10))
    break_thr = float(params.get("break_threshold_ticks", 4)) * float(params.get("tick", 0.10))
    max_retest = int(params.get("max_retest_bars", 24))
    min_body_ratio = float(params.get("min_body_ratio", 0.0))
    require_sweep_wick = int(params.get("require_sweep_wick", 0))
    dirs = str(params.get("dirs", "both"))
    session = str(params.get("session", "all"))
    trend_ema = int(params.get("trend_ema", 0))
    ts_v = bars["ts"].values

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    last = n - 1
    if last < 30:
        return None

    if session != "all":
        try:
            hour = int(pd.Timestamp(ts_v[last]).hour)
            minute = int(pd.Timestamp(ts_v[last]).minute)
        except Exception:
            return None
        t = hour + minute / 60.0
        if session == "ny_am" and not (13.5 <= t < 16.0):
            return None
        if session == "london" and not (7.0 <= t < 10.0):
            return None
        if session == "ny_pm" and not (17.0 <= t < 20.0):
            return None
        if session == "asia" and not (22.0 <= t or t < 3.0):
            return None

    ema = None
    if trend_ema > 0:
        if n < trend_ema + 2:
            return None
        ema = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().values[last]

    pdh = pd.Series(h).rolling(rolling_window, min_periods=8).max().shift(1).values
    pdl = pd.Series(l).rolling(rolling_window, min_periods=8).min().shift(1).values
    if np.isnan(pdh[last]) or np.isnan(pdl[last]):
        return None

    def _body_ratio(i):
        rng = h[i] - l[i]
        if rng <= 0:
            return 0.0
        return abs(c[i] - o[i]) / rng

    def _trend_ok(direction):
        if ema is None:
            return True
        if direction == "long":
            return c[last] > ema
        return c[last] < ema

    for direction in ("long", "short"):
        if dirs != "both" and direction != dirs:
            continue
        if direction == "long":
            if not (h[last] >= pdh[last] + break_thr):
                continue
        else:
            if not (l[last] <= pdl[last] - break_thr):
                continue
        if min_body_ratio > 0 and _body_ratio(last) < min_body_ratio:
            continue
        if not _trend_ok(direction):
            continue
        for j in range(max(0, last - max_retest), last):
            if direction == "long":
                if not (c[j] < pdh[j] and l[j] >= pdl[j]):
                    continue
            else:
                if not (c[j] > pdl[j] and h[j] <= pdh[j]):
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
                # ---- HONEST ENTRY: the decision-bar close ---------------
                entry = c[last]
                # ------------------------------------------------------
                if direction == "long":
                    stop = min(l[j], l[last]) - stop_buf
                    risk = entry - stop
                    if risk < 0.5 * float(params.get("tick", 0.10)):
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
                    if risk < 0.5 * float(params.get("tick", 0.10)):
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

# --------------------------------------------------------------------------
# TEMPLATE 2 — honest ATR displacement continuation.
#   mode 'close'    : enter at the displacement bar's close (decision bar)
#   mode 'pullback' : wait for a retrace bar, enter at ITS close
# The house version (csid 262) enters at `h - 0.40*range` of the
# displacement bar itself -- a price that had not been reached yet at the
# moment the decision was made. That is the fiction.
# --------------------------------------------------------------------------
CODE_ATR = r'''
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None
    atr_period = int(params.get("atr_period", 14))
    disp_mult = float(params.get("disp_atr_mult", 1.3))
    body_ratio_min = float(params.get("body_ratio_min", 0.70))
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buffer_atr", 0.15))
    entry_mode = str(params.get("entry_mode", "pullback"))
    max_pullback = int(params.get("max_pullback_bars", 5))
    min_retrace_frac = float(params.get("min_retrace_frac", 0.30))
    dirs = str(params.get("dirs", "both"))
    session = str(params.get("session", "all"))
    trend_ema = int(params.get("trend_ema", 0))
    ts_v = bars["ts"].values

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    last = n - 1
    if last < atr_period + 10:
        return None

    if session != "all":
        try:
            ts = pd.Timestamp(ts_v[last])
            t = ts.hour + ts.minute / 60.0
        except Exception:
            return None
        if session == "ny_am" and not (13.5 <= t < 16.0):
            return None
        if session == "london" and not (7.0 <= t < 10.0):
            return None
        if session == "ny_pm" and not (17.0 <= t < 20.0):
            return None

    prev_c = c[:last]
    tr = np.maximum(h[1:last] - l[1:last],
                    np.maximum(np.abs(h[1:last] - prev_c[:-1]),
                               np.abs(l[1:last] - prev_c[:-1])))
    if len(tr) < atr_period:
        return None
    atr = float(tr[-atr_period:].mean())
    if atr <= 0:
        return None

    ema = None
    if trend_ema > 0:
        if n < trend_ema + 2:
            return None
        ema = pd.Series(c).ewm(span=trend_ema, adjust=False).mean().values[last]

    # Locate candidate displacement bars.  The DECISION bar must be the bar
    # on which the entry is taken, otherwise the fill is delayed and the
    # signal is stale (the same defect that made the house family fiction).
    #   'close'    -> the displacement bar IS the decision bar
    #   'pullback' -> the CURRENT bar is the retrace of a recent displacement
    cands = []
    for i in range(last, max(0, last - int(params.get("max_pullback_bars", 5)) - 1), -1):
        rng = h[i] - l[i]
        if rng <= 0:
            continue
        br = abs(c[i] - o[i]) / rng
        if br >= body_ratio_min and rng >= disp_mult * atr:
            cands.append(i)
    if not cands:
        return None
    if entry_mode == "close":
        if cands[0] != last:
            return None
        disp_i = last
        is_bull = c[disp_i] > o[disp_i]
        direction = "long" if is_bull else "short"
        if dirs != "both" and direction != dirs:
            return None
        if ema is not None:
            if direction == "long" and not (c[last] > ema):
                return None
            if direction == "short" and not (c[last] < ema):
                return None

    if entry_mode == "close":
        entry = c[last]
        if direction == "long":
            stop = l[disp_i] - stop_buf_atr * atr
            risk = entry - stop
            if risk <= 0:
                return None
            target = entry + risk * target_r
            return {"ts": ts_v[last], "direction": "long", "entry": float(entry),
                    "stop": float(stop), "target": float(target)}
        stop = h[disp_i] + stop_buf_atr * atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - risk * target_r
        return {"ts": ts_v[last], "direction": "short", "entry": float(entry),
                "stop": float(stop), "target": float(target)}

    # entry_mode == 'pullback': the CURRENT bar must be the retrace bar of a
    # recent displacement bar.  The entry is that bar's close, so the
    # decision bar == the signal bar == the fill bar.
    for disp_i in cands:
        is_bull = c[disp_i] > o[disp_i]
        direction = "long" if is_bull else "short"
        if dirs != "both" and direction != dirs:
            continue
        if ema is not None:
            if direction == "long" and not (c[last] > ema):
                continue
            if direction == "short" and not (c[last] < ema):
                continue
        disp_rng = h[disp_i] - l[disp_i]
        if disp_rng <= 0:
            continue
        mid = l[disp_i] + 0.5 * disp_rng
        j = last
        if direction == "long":
            if h[j] > h[disp_i]:
                continue
            depth = (h[disp_i] - l[j]) / disp_rng
            if depth < min_retrace_frac:
                continue
            if c[j] < mid:
                continue
            entry = c[j]
            stop = min(l[disp_i], l[j]) - stop_buf_atr * atr
            risk = entry - stop
            if risk <= 0:
                continue
            target = entry + risk * target_r
            return {"ts": ts_v[j], "direction": "long", "entry": float(entry),
                    "stop": float(stop), "target": float(target)}
        else:
            if l[j] < l[disp_i]:
                continue
            depth = (h[j] - l[disp_i]) / disp_rng
            if depth < min_retrace_frac:
                continue
            if c[j] > mid:
                continue
            entry = c[j]
            stop = max(h[disp_i], h[j]) + stop_buf_atr * atr
            risk = stop - entry
            if risk <= 0:
                continue
            target = entry - risk * target_r
            return {"ts": ts_v[j], "direction": "short", "entry": float(entry),
                    "stop": float(stop), "target": float(target)}
    return None
'''

TEMPLATES = {"sb": CODE_SB, "atr": CODE_ATR}

# tick / dollars-per-point per symbol (production contract specs)
SPEC = {s: (float(v["tick"]), float(v["tick_value"]) / float(v["tick"]))
        for s, v in _CONTRACT_SPECS.items()}


# --------------------------------------------------------------------------
def run_fast(code, bars, params, symbol, dpp, mode, tail=TAIL):
    """Mirror of fair_entry_restamp_book2.run_variant with a bounded window.

    modes: 'base'  -> fill at the strategy's own entry price (fiction check)
           'fair'  -> fill at the decision bar close
           'live'  -> fill at the next bar open, stops/targets re-anchored
    """
    fn, meta = _compile_and_load(code)
    if fn is None:
        return None, {"load_error": meta.get("error")}
    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    trades = []
    open_t = None
    n_signals = 0
    eval_errors = 0
    t0 = time.time()
    for i in range(2, n):
        if open_t is not None:
            d = open_t["direction"]
            o, h, l = O[i], H[i], L[i]
            stop_hit = (l <= open_t["stop"]) if d == "long" else (h >= open_t["stop"])
            tgt_hit = (h >= open_t["target"]) if d == "long" else (l <= open_t["target"])
            if stop_hit or tgt_hit:
                if mode == "base":
                    if stop_hit:
                        px, reason = open_t["stop"], "stop"
                    else:
                        px, reason = open_t["target"], "target"
                else:
                    if d == "long":
                        if stop_hit and o <= open_t["stop"]:
                            px, reason = o, "stop_gap"
                        elif stop_hit:
                            px, reason = open_t["stop"], "stop"
                        elif o >= open_t["target"]:
                            px, reason = o, "target_gap"
                        else:
                            px, reason = open_t["target"], "target"
                    else:
                        if stop_hit and o >= open_t["stop"]:
                            px, reason = o, "stop_gap"
                        elif stop_hit:
                            px, reason = open_t["stop"], "stop"
                        elif o <= open_t["target"]:
                            px, reason = o, "target_gap"
                        else:
                            px, reason = open_t["target"], "target"
                e = open_t["entry"]
                gross = (px - e) if d == "long" else (e - px)
                from src.tools.cost_model import apply_costs_to_trade
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross * dpp,
                                           exit_reason="target" if "target" in reason else "stop")
                risk = abs(e - open_t["stop"]) or 1.0
                trades.append({"entry_bar": open_t["entry_bar"], "exit_bar": i,
                               "direction": d, "entry": round(e, 6), "px": round(px, 6),
                               "reason": reason, "pnl": float(pnl),
                               "r": float(gross / risk), "bars_held": i - open_t["entry_bar"]})
                open_t = None
            continue
        try:
            sig = fn(bars.iloc[max(0, i - tail + 1): i + 1], params)
        except Exception:  # noqa: BLE001
            eval_errors += 1
            continue
        if not isinstance(sig, dict):
            continue
        d = str(sig.get("direction", "")).lower()
        if d not in ("long", "short"):
            continue
        try:
            s_entry = float(sig["entry"])
            s_stop = float(sig["stop"])
            s_target = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(s_entry) and math.isfinite(s_stop) and math.isfinite(s_target)):
            continue
        sd = abs(s_entry - s_stop)
        td = abs(s_target - s_entry)
        if sd <= 0 or td <= 0:
            continue
        n_signals += 1
        if mode == "base":
            entry = s_entry
        elif mode == "live":
            if i + 1 >= n:
                continue
            entry = O[i + 1]
        else:
            entry = C[i]
        sign = 1.0 if d == "long" else -1.0
        open_t = {"direction": d, "entry": entry, "stop": entry - sign * sd,
                  "target": entry + sign * td, "entry_bar": i + 1}
    meta2 = {"bars_replayed": n, "n_bars": n, "n_signals": n_signals,
             "eval_errors": eval_errors, "loop_s": round(time.time() - t0, 2)}
    return trades, meta2


def evaluate_config(code, bars, params, symbol, modes=("live", "fair")):
    dpp = SPEC[symbol][1]
    out = {}
    for mode in modes:
        trades, meta = run_fast(code, bars, params, symbol, dpp, mode)
        m = metrics(trades)
        m.update({k: meta[k] for k in ("n_signals", "eval_errors", "loop_s") if k in meta})
        out[mode] = m
    return out


# --------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="1")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    pairs = [("MGC", "15m"), ("MNQ", "15m"), ("6J", "15m"),
             ("MGC", "5m"), ("MNQ", "5m"), ("MCL", "5m"), ("6J", "5m"),
             ("MGC", "1h"), ("ZF", "1h")]
    bars_cache = {}
    rows = []

    if args.stage == "1":
        for sym, tf in pairs:
            try:
                bars = bar_archive.read_archive(sym, tf)
            except Exception as exc:  # noqa: BLE001
                print(f"[{sym} {tf}] archive error {exc}")
                continue
            if bars is None or len(bars) < 500:
                print(f"[{sym} {tf}] no archive")
                continue
            bars_cache[(sym, tf)] = bars
            tick = SPEC[sym][0]
            grid = []
            for rolling, tR, brm, sbuf in itertools.product(
                    (12, 24, 48), (1.0, 1.5, 2.0, 3.0), (0.0, 0.5), (4.0, 6.0)):
                grid.append({"rolling_pdhl_bars": rolling, "target_r": tR,
                             "min_body_ratio": brm, "stop_buf_ticks": sbuf,
                             "break_threshold_ticks": 4, "max_retest_bars": 24,
                             "require_sweep_wick": 0, "dirs": "both",
                             "session": "all", "trend_ema": 0, "tick": tick})
            t0 = time.time()
            for params in grid:
                res = evaluate_config(CODE_SB, bars, params, sym)
                rows.append({"template": "sb", "symbol": sym, "tf": tf,
                             "params": params, "LIVE": res["live"], "FAIR": res["fair"]})
            print(f"[{sym} {tf}] sb core grid {len(grid)} combos in {time.time()-t0:.1f}s")
            sys.stdout.flush()

            grid = []
            for dm, brm, tR, mode in itertools.product(
                    (1.0, 1.3, 1.6), (0.70, 0.80), (1.0, 1.5, 2.0), ("pullback", "close")):
                grid.append({"atr_period": 14, "disp_atr_mult": dm,
                             "body_ratio_min": brm, "target_r": tR,
                             "stop_buffer_atr": 0.15, "entry_mode": mode,
                             "max_pullback_bars": 5, "min_retrace_frac": 0.30,
                             "dirs": "both", "session": "all", "trend_ema": 0})
            t0 = time.time()
            for params in grid:
                res = evaluate_config(CODE_ATR, bars, params, sym)
                rows.append({"template": "atr", "symbol": sym, "tf": tf,
                             "params": params, "LIVE": res["live"], "FAIR": res["fair"]})
            print(f"[{sym} {tf}] atr core grid {len(grid)} combos in {time.time()-t0:.1f}s")
            sys.stdout.flush()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"cost_model": COST_MODEL_VERSION, "rows": rows},
              open(args.out, "w"), indent=1, default=str)

    # ---- ranked table -----------------------------------------------------
    good = [r for r in rows
            if r["LIVE"].get("n", 0) >= 20 and (r["LIVE"].get("pf") or 0) > 1.0
            and (r["FAIR"].get("pf") or 0) > 1.0]
    good.sort(key=lambda r: -(r["LIVE"].get("pf") or 0))
    print("\n=== HONEST SURVIVORS (LIVE PF>1 and FAIR PF>1, n>=20) ===")
    for r in good[:40]:
        L, F = r["LIVE"], r["FAIR"]
        print(f"{r['template']:4} {r['symbol']:4} {r['tf']:4} n={L['n']:>4} "
              f"livePF={L['pf']:>6.3f} liveWR={L['wr_pct']:>5.1f} "
              f"fairPF={F['pf']:>6.3f} eR={L['e_r']:>6.3f} "
              f"usd/t={L['usd_per_trade']:>7.2f} p={L['p_1sided']:.4f} "
              f"dd={L['max_dd']:>8.2f} | {r['params']}")
    if not good:
        print("(none)")


if __name__ == "__main__":
    main()
