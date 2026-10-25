"""ICT SILVER BULLET — VIDEO SPEC (honest fill) — 2026-10-24.

SOURCE
------
YT:X7kvlngAjY4 "ICT Silver Bullet Strategy: The Complete Guide (Tested on 1
Year of Gold & NQ)" (channel "Edge or Myth"). The author actually BACKTESTED
it and published the numbers, which is rare and worth taking seriously:

    every setup, all three windows, both markets   1,128 trades, 38% WR, +$1,890
    ... only the setups WITH the day (midnight line)  362 trades, 42% WR, +$4,090
    best single window: 10:00-11:00 ET  (half the trades win, +$3,060 on its own)

THE FOUR CHECKS (as stated in the video)
-----------------------------------------
  1. WINDOW  -- one of three one-hour windows: 3-4 ET, 10-11 ET, 2-3 PM ET.
                Outside them: no trade.
  2. LINE    -- mark the HIGH and LOW of the hour BEFORE the window.  Above the
                high is a pile of buy-stops, below the low a pile of sell-stops.
  3. SWEEP   -- inside the window price pokes above that high (or below that
                low) and then SNAPS BACK.  "Price keeps going and never comes
                back, it's not a sweep. No trade."
  4. GAP     -- after the sweep, three candles where the middle one moves so
                fast that candle one and candle three never touch -> FVG.
                Price comes back into that gap -> THAT is the entry.
                Stop just beyond the sweep extreme, target 2R.
  5. THE LINE THAT CHANGES EVERYTHING -- at midnight New York time mark the
     opening price.  Below it the day is falling (sells only), above it the
     day is rising (buys only).

WHY THIS IS WORTH A BACKTEST
----------------------------
The house silver-bullet family uses a ROLLING N-bar PDH/PDL (12/24/48 bars),
no time window, no FVG requirement and no daily bias -- and, per finding
#9133/#9134, it fills at a price the market had already left.  This spec is
a different, sharper hypothesis: a FIXED prior-hour reference level, a
hard time window, a mandatory imbalance, and a directional daily filter.

THE FILL IS HONEST
------------------
The entry is the CLOSE OF THE DECISION BAR (the bar on which price touches
the gap edge).  That price was traded at the instant of the decision, so a
market order filled there is achievable.  The harness measures the same
config under three fills:
    base = the strategy's own entry price (what run_custom_backtest reports)
    fair = decision bar close
    live = next bar open, stops/targets RE-ANCHORED, gap-aware exits
`live` is the verdict number; `base` is the honesty check.

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
from fair_entry_restamp_book2_20261022 import _compile_and_load, metrics  # noqa: E402
from honest_edge_hunt_20261024 import run_fast  # noqa: E402

OUT = "outputs/ict_sb_video_spec_hunt_20261024.json"

# --------------------------------------------------------------------------
# The strategy, written exactly to the video's four checks.
# --------------------------------------------------------------------------
CODE_SB2 = r'''
import numpy as np
import pandas as pd
import datetime


def _dst_offset(utc_ts):
    """US Eastern offset in hours for a naive-UTC pandas Timestamp.

    DST runs from the 2nd Sunday of March to the 1st Sunday of November.
    Returns -4 (EDT) or -5 (EST).
    """
    y = int(utc_ts.year)
    mar1 = datetime.date(y, 3, 1)
    dst_start = datetime.date(y, 3, 1 + (6 - mar1.weekday()) % 7)
    nov1 = datetime.date(y, 11, 1)
    dst_end = datetime.date(y, 11, 1 + (6 - nov1.weekday()) % 7)
    d = utc_ts.date()
    return -4 if dst_start <= d < dst_end else -5


def evaluate(bars, params):
    if bars is None or len(bars) < 80:
        return None
    tick = float(params.get("tick", 0.10))
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf_ticks", 2)) * tick
    windows = params.get("windows_et")
    if windows is None:
        windows = [[10, 11]]
    use_bias = int(params.get("use_daily_bias", 1))
    require_snap = int(params.get("require_snapback", 1))
    entry_slack = int(params.get("entry_window_slack_bars", 0))
    require_fvg = int(params.get("require_fvg", 1))

    ts_v = bars["ts"].values
    n = len(bars)
    last = n - 1
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    off = _dst_offset(pd.Timestamp(ts_v[last]))
    tsn = ts_v.astype("datetime64[m]").astype("int64")
    etm = tsn + off * 60
    et_day = etm // 1440
    et_hour = (etm % 1440) // 60

    day = int(et_day[last])
    hour = int(et_hour[last])
    win_start = None
    for w in windows:
        if hour == int(w[0]):
            win_start = int(w[0])
            break
    if win_start is None:
        return None

    # ---- CHECK 2: the LINE -- high/low of the hour BEFORE the window -----
    prev_hour = (win_start - 1) % 24
    ref_idx = np.where((et_hour == prev_hour) & (et_day == day))[0]
    if len(ref_idx) == 0:
        return None
    ref_high = float(h[ref_idx].max())
    ref_low = float(l[ref_idx].min())

    win_idx = np.where((et_hour == win_start) & (et_day == day))[0]
    if len(win_idx) == 0:
        return None

    # ---- the midnight line (daily bias) --------------------------------
    mid_open = None
    if use_bias:
        day_idx = np.where(et_day == day)[0]
        if len(day_idx) == 0:
            return None
        mid_open = float(o[day_idx[0]])

    for pos in range(len(win_idx)):
        i = int(win_idx[pos])
        if h[i] > ref_high:
            direction = "short"
            sweep_extreme = float(h[i])
        elif l[i] < ref_low:
            direction = "long"
            sweep_extreme = float(l[i])
        else:
            continue

        # ---- CHECK 3: the SNAPBACK -------------------------------------
        snap_i = i
        if require_snap:
            snap_i = None
            for j in win_idx[pos + 1:]:
                if direction == "short" and c[j] < ref_high:
                    snap_i = int(j)
                    break
                if direction == "long" and c[j] > ref_low:
                    snap_i = int(j)
                    break
            if snap_i is None:
                continue

        # ---- CHECK 4: the GAP (3-candle imbalance) ---------------------
        # bullish FVG at bar f: low[f] > high[f-1];  bearish: high[f] < low[f-1]
        for f in range(snap_i + 1, last):
            if f + 1 > last:
                break
            if direction == "long" and l[f] > h[f - 1]:
                gap_edge = float(h[f - 1])
                lo = f + 2
                hi = min(last, lo + entry_slack + 1)
                for j in range(lo, hi + 1):
                    if l[j] <= gap_edge:
                        entry = float(c[j])
                        stop = sweep_extreme - stop_buf
                        risk = entry - stop
                        if risk < 0.5 * tick:
                            continue
                        if use_bias and not (entry > mid_open):
                            continue
                        target = entry + risk * target_r
                        return {"ts": ts_v[j], "direction": "long",
                                "entry": entry, "stop": float(stop),
                                "target": float(target)}
            if direction == "short" and h[f] < l[f - 1]:
                gap_edge = float(h[f])
                lo = f + 2
                hi = min(last, lo + entry_slack + 1)
                for j in range(lo, hi + 1):
                    if h[j] >= gap_edge:
                        entry = float(c[j])
                        stop = sweep_extreme + stop_buf
                        risk = stop - entry
                        if risk < 0.5 * tick:
                            continue
                        if use_bias and not (entry < mid_open):
                            continue
                        target = entry - risk * target_r
                        return {"ts": ts_v[j], "direction": "short",
                                "entry": entry, "stop": float(stop),
                                "target": float(target)}
        if not require_fvg:
            # fallback: enter at the snapback close (no imbalance required)
            j = snap_i
            entry = float(c[j])
            if direction == "long":
                stop = sweep_extreme - stop_buf
                risk = entry - stop
                if risk < 0.5 * tick:
                    continue
                if use_bias and not (entry > mid_open):
                    continue
                target = entry + risk * target_r
                return {"ts": ts_v[j], "direction": "long", "entry": entry,
                        "stop": float(stop), "target": float(target)}
            stop = sweep_extreme + stop_buf
            risk = stop - entry
            if risk < 0.5 * tick:
                continue
            if use_bias and not (entry < mid_open):
                continue
            target = entry - risk * target_r
            return {"ts": ts_v[j], "direction": "short", "entry": entry,
                    "stop": float(stop), "target": float(target)}
    return None
'''

SPEC = {s: (float(v["tick"]), float(v["tick_value"]) / float(v["tick"]))
        for s, v in _CONTRACT_SPECS.items()}


def evaluate_config(code, bars, params, symbol, modes=("live", "fair", "base")):
    dpp = SPEC[symbol][1]
    out = {}
    for mode in modes:
        trades, meta = run_fast(code, bars, params, symbol, dpp, mode)
        m = metrics(trades)
        m["n_signals"] = meta.get("n_signals")
        m["eval_errors"] = meta.get("eval_errors")
        out[mode] = m
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--pairs", default="MGC:15m,MNQ:15m,MGC:5m,MNQ:5m,MCL:5m,6J:15m,6J:5m,MGC:1h,MNQ:1h")
    args = ap.parse_args()

    rows = []
    for pair in args.pairs.split(","):
        sym, tf = pair.split(":")
        try:
            bars = bar_archive.read_archive(sym, tf)
        except Exception as exc:  # noqa: BLE001
            print(f"[{sym} {tf}] archive error: {exc}")
            continue
        if bars is None or len(bars) < 500:
            print(f"[{sym} {tf}] no archive")
            continue
        tick = SPEC[sym][0]
        grid = []
        for wins, bias, tR, slack in itertools.product(
                ([[10, 11]], [[3, 4], [10, 11], [14, 15]]),
                (1, 0),
                (1.5, 2.0, 3.0),
                (0, 4, 12)):
            grid.append({"windows_et": wins, "use_daily_bias": bias,
                         "target_r": tR, "require_snapback": 1,
                         "require_fvg": 1, "entry_window_slack_bars": slack,
                         "stop_buf_ticks": 2, "tick": tick})
        t0 = time.time()
        for params in grid:
            res = evaluate_config(CODE_SB2, bars, params, sym)
            rows.append({"symbol": sym, "tf": tf, "params": params,
                         "LIVE": res["live"], "FAIR": res["fair"], "BASE": res["base"]})
        print(f"[{sym} {tf}] {len(grid)} configs in {time.time()-t0:.1f}s")
        sys.stdout.flush()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"cost_model": COST_MODEL_VERSION, "rows": rows},
              open(args.out, "w"), indent=1, default=str)

    good = [r for r in rows if r["LIVE"].get("n", 0) >= 20 and (r["LIVE"].get("pf") or 0) > 1.0]
    good.sort(key=lambda r: -(r["LIVE"].get("pf") or 0))
    print("\n=== VIDEO-SPEC HONEST SURVIVORS (LIVE PF>1, n>=20) ===")
    for r in good[:30]:
        L, F, B = r["LIVE"], r["FAIR"], r["BASE"]
        p = r["params"]
        print(f"{r['symbol']:4} {r['tf']:4} n={L['n']:>4} livePF={L['pf']:>6.3f} "
              f"liveWR={L['wr_pct']:>5.1f} fairPF={F['pf']:>6.3f} basePF={B['pf']:>6.3f} "
              f"eR={L['e_r']:>6.3f} usd/t={L['usd_per_trade']:>7.2f} p={L['p_1sided']:.4f} "
              f"| wins={p['windows_et']} bias={p['use_daily_bias']} tR={p['target_r']} "
              f"slack={p['entry_window_slack_bars']}")
    if not good:
        print("(none)")


if __name__ == "__main__":
    main()
