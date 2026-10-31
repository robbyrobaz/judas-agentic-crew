"""HONEST AUDIT of the session-gated market-entry sweep->displacement result.

Three questions, in order of importance:

1. DATA INTEGRITY.  The archive is NOT continuous (MGC 15m has ~50 bars/day
   over 350 calendar days).  If the "session gate" is really selecting the
   data-dense hours, the edge is an artefact.  If the fills are landing many
   hours after the decision (because the next bar in the array is hours
   away), the production fill is unrealistically kind to us.
2. IS THE EDGE REAL?  PF by decision-hour for the UNGATED family -- if the
   06:00-12:00 UTC band is where the money is in the ungated family too,
   then the gate is just removing losing hours (legitimate, implementable).
3. IS IT OVERFIT?  Nested (leave-one-fold-out) SELECTION of the session
   window: pick the window on the other folds, score it on the held-out
   fold.  A knife-edge overfit fails this; a broad band survives it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

from explore_prod import (_CONTRACT_SPECS, apply_costs_to_trade,  # noqa: E402
                          load_bars, sigs_sweep_disp, split_report)
from coord_search import BASE  # noqa: E402
from src.tools.cost_model import round_trip_cost  # noqa: E402

LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
WINDOWS = [(-1, -1), (4, 10), (5, 12), (6, 11), (6, 12), (7, 12), (7, 13),
           (8, 12), (12, 18), (12, 21), (13, 20), (6, 16)]
FOLDS = [(0.40, 0.20), (0.60, 0.20), (0.80, 0.20)]


def simulate_detail(bars: pd.DataFrame, sigs: dict, symbol: str, qty: int = 1) -> dict:
    """PRODUCTION fill, but every trade is recorded (decision idx, fill idx,
    fill gap in minutes, pnl, reason)."""
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    TS = bars["ts"].dt.tz_convert("UTC")
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    n = len(bars)
    trades = []
    open_trade = None
    pending = None
    for i in range(2, n):
        if pending is not None:
            d, stop, target, dec = pending
            open_trade = {"direction": d, "entry": float(O[i]), "stop": float(stop),
                          "target": float(target), "dec": dec, "fill": i}
            pending = None
        if open_trade is not None:
            d = open_trade["direction"]
            e, s, t = open_trade["entry"], open_trade["stop"], open_trade["target"]
            res = None
            if d == "long":
                if O[i] <= s:
                    res = (O[i] - e, "stop")
                elif L[i] <= s:
                    res = (s - e, "stop")
                elif H[i] >= t:
                    res = (t - e, "target")
            else:
                if O[i] >= s:
                    res = (e - O[i], "stop")
                elif H[i] >= s:
                    res = (e - s, "stop")
                elif L[i] <= t:
                    res = (e - t, "target")
            if res is not None:
                pts, reason = res
                gross = pts * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason=reason, qty=qty)
                risk = abs(e - s) or 1e-9
                gap = (TS.iloc[i] - TS.iloc[open_trade["dec"]]).total_seconds() / 60.0
                trades.append({"dec": open_trade["dec"], "fill": i, "gap_min": gap,
                               "pnl": float(pnl), "reason": reason,
                               "r": float(pnl / (risk * dpp * qty)),
                               "hour": int(TS.iloc[open_trade["dec"]].hour),
                               "date": str(TS.iloc[open_trade["dec"]].date())})
                open_trade = None
            continue
        if i in sigs:
            pending = (*sigs[i], i)
    pnls = [t["pnl"] for t in trades]
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = float(gw / gl) if gl > 0 else (0.0 if gw == 0 else 999.0)
    return {"trades": trades, "n": len(pnls), "pf": round(pf, 4),
            "pnl": round(float(sum(pnls)), 2),
            "expectancy_r": round(float(np.mean([t["r"] for t in trades])), 4) if trades else 0.0}


def fold_pf(bars, sigs, symbol):
    n = len(bars)
    out = []
    for (tr_end, size) in FOLDS:
        a = int(n * tr_end)
        b = min(int(n * (tr_end + size)), n)
        sub = bars.iloc[a:b].reset_index(drop=True)
        ss = {i - a: v for i, v in sigs.items() if a <= i < b}
        out.append(simulate_detail(sub, ss, symbol))
    return out


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    bars = load_bars(symbol, tf, None)
    n = len(bars)
    ts = bars["ts"].dt.tz_convert("UTC")
    print(f"### {symbol} {tf}: {n} bars {bars['ts'].iloc[0]} -> {bars['ts'].iloc[-1]}")
    gaps = ts.diff().dt.total_seconds().div(60).dropna()
    print(f"bar gap (min): median={gaps.median():.0f} p90={gaps.quantile(.9):.0f} "
          f"max={gaps.max():.0f}  frac>15min={float((gaps > 15).mean()):.3f} "
          f"frac>60min={float((gaps > 60).mean()):.3f}")
    per_hour = ts.dt.hour.value_counts().reindex(range(24), fill_value=0)
    print("bars/hour:", {int(h): int(c) for h, c in per_hour.items()})

    base_cfg = dict(BASE, _tf=tf)
    base_sigs = sigs_sweep_disp(bars, base_cfg)
    det = simulate_detail(bars, base_sigs, symbol)
    print(f"\nBASE (no session gate): n={det['n']} pf={det['pf']} pnl=${det['pnl']} "
          f"er={det['expectancy_r']}")
    gaps_f = pd.Series([t["gap_min"] for t in det["trades"]])
    print(f"  fill gap (min): median={gaps_f.median():.0f} p90={gaps_f.quantile(.9):.0f} "
          f"max={gaps_f.max():.0f}  frac>15min={float((gaps_f > 15).mean()):.3f}")
    near = [t for t in det["trades"] if t["gap_min"] <= 15]
    far = [t for t in det["trades"] if t["gap_min"] > 15]
    for label, sub in (("gap<=15min", near), ("gap>15min", far)):
        if sub:
            p = [t["pnl"] for t in sub]
            gw = sum(x for x in p if x > 0)
            gl = -sum(x for x in p if x < 0)
            pf = gw / gl if gl > 0 else 999.0
            print(f"  {label}: n={len(sub)} pf={pf:.3f} pnl=${sum(p):.2f}")

    print("\nPF BY DECISION HOUR (ungated family):")
    df = pd.DataFrame(det["trades"])
    rows = []
    for h, g in df.groupby("hour"):
        gw = g.loc[g.pnl > 0, "pnl"].sum()
        gl = -g.loc[g.pnl <= 0, "pnl"].sum()
        rows.append((int(h), len(g), round(gw / gl, 3) if gl > 0 else 999.0,
                     round(float(g.pnl.sum()), 2)))
    for r in sorted(rows):
        print(f"  hour {r[0]:>2}: n={r[1]:>3} pf={r[2]:>7} pnl=${r[3]:>10}")

    print("\nNESTED SESSION-WINDOW SELECTION (leave-one-fold-out):")
    wf = {}
    for (lo, hi) in WINDOWS:
        cfg = dict(base_cfg, sess_lo=lo, sess_hi=hi)
        wf[(lo, hi)] = fold_pf(bars, sigs_sweep_disp(bars, cfg), symbol)
    for k, (lo, hi) in enumerate(WINDOWS):
        pick_pf, pick_n, base_pf, base_n = 0.0, 0, 0.0, 0
        for k2 in range(3):
            # choose the window that maximises PF on the OTHER two folds
            best, bestpf = None, -1.0
            for w, folds in wf.items():
                pf_other = np.mean([folds[j]["pf"] for j in range(3) if j != k2])
                if pf_other > bestpf:
                    best, bestpf = w, pf_other
            held = wf[best][k2]
            bl = wf[(-1, -1)][k2]
            pick_pf += held["pf"]
            pick_n += held["n"]
            base_pf += bl["pf"]
            base_n += bl["n"]
            print(f"  fold{k2} picked={best} -> heldout pf={held['pf']:.3f} n={held['n']:3d} "
                  f"| ungated pf={bl['pf']:.3f} n={bl['n']:3d}")
        print(f"  window {lo}-{hi}: nested mean heldout pf={pick_pf/3:.3f} "
              f"n={pick_n} vs ungated {base_pf/3:.3f} n={base_n}")


if __name__ == "__main__":
    main()
