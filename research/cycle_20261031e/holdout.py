"""IS / TRUE-HOLDOUT validation of the session-gated sweep->displacement family.

WHY THIS FILE
-------------
Cycle 20261031a/d selected configs with a 3-fold chronological walk-forward over
the FULL archive (2025-11 -> 2026-10 for MGC 15m).  Every "OOS fold" was
therefore still inside the window the search had already looked at, so those
numbers are a SELECTION-BIASED estimate, not an out-of-sample one.

This file runs the protocol properly:

    IS      = the RECENT window  (2026-06-11 -> 2026-10-26 for MGC 15m)
              -- the ONLY data the config search is allowed to see.
    HOLDOUT = the OLDER window   (2025-11-12 -> 2026-06-10)
              -- never scored during selection; it is scored ONCE, at the end,
                 for the configs the IS search picked.

Scoring is the PRODUCTION fill (entry = open of the bar after the decision,
stop wins ties, a stop already breached at the open fills at the open) with
v1_realistic_micros costs -- i.e. exactly what the runtime does.

Two hygiene modes are scored, because the LIVE scanner only ever sees bars
that printed:
    default : filler dropped (volume==0 AND range==0), single-print kept
    strict  : additionally drops every zero-range bar
An edge that only survives in one mode is fragile and is reported as such.

Usage:
    .venv/bin/python research/cycle_20261031e/holdout.py MGC 15m
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research/cycle_20261031a"))

from explore_prod import load_bars, sigs_sweep_disp, simulate_prod  # noqa: E402
from coord_search import BASE  # noqa: E402

# IS / HOLDOUT boundary per symbol+tf.  The HOLDOUT side is the OLDER data
# (never seen by the 20261031a search, which ran on the full series).
SPLITS = {
    ("MGC", "15m"): ("2026-06-11", "2026-10-27"),
    ("MNQ", "15m"): ("2026-06-11", "2026-10-27"),
    ("MCL", "15m"): ("2026-06-11", "2026-10-28"),
    ("6J", "15m"): ("2026-06-11", "2026-10-27"),
    ("ZF", "15m"): ("2026-06-11", "2026-10-27"),
    ("MGC", "5m"): ("2026-06-11", "2026-10-29"),
    ("MNQ", "5m"): ("2026-06-11", "2026-10-22"),
    ("MCL", "5m"): ("2026-06-11", "2026-10-24"),
    ("ZF", "5m"): ("2026-06-11", "2026-10-30"),
    ("6J", "5m"): ("2026-06-11", "2026-10-24"),
}

# Config axes searched ON THE IS WINDOW ONLY.
SESSIONS = [(-1, -1), (4, 10), (5, 12), (6, 11), (6, 12), (6, 13), (7, 12),
            (7, 13), (8, 12), (12, 18), (12, 21), (13, 20), (7, 16)]
DISP = [0.8, 1.0, 1.25]
RR = [1.0, 1.5, 2.0]
BODY = [0.45, 0.55, 0.65]


def strict_hygiene(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars[(bars["high"] - bars["low"]) > 0]
    if "volume" in b.columns:
        b = b[b["volume"] > 0]
    return b.reset_index(drop=True)


def slice_window(bars: pd.DataFrame, lo: str, hi: str) -> pd.DataFrame:
    ts = bars["ts"]
    m = (ts >= pd.Timestamp(lo, tz="UTC")) & (ts < pd.Timestamp(hi, tz="UTC"))
    return bars.loc[m].reset_index(drop=True)


def score(bars: pd.DataFrame, cfg: dict, tf: str, symbol: str) -> dict:
    sigs = sigs_sweep_disp(bars, dict(cfg, _tf=tf))
    r = simulate_prod(bars, sigs, symbol)
    r["n_signals"] = len(sigs)
    return r


def per_month(bars: pd.DataFrame, sigs: dict, symbol: str) -> list:
    ts = bars["ts"]
    buckets: dict = {}
    for i in sigs:
        buckets.setdefault(ts.iloc[i].strftime("%Y-%m"), []).append(i)
    out = []
    for k in sorted(buckets):
        sub = {i: sigs[i] for i in buckets[k]}
        r = simulate_prod(bars, sub, symbol)
        out.append((k, r["n_trades"], r["pf"], r["total_pnl"]))
    return out


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "15m"
    raw = load_bars(symbol, tf, None)
    print(f"# {symbol} {tf}: {len(raw)} bars  {raw['ts'].iloc[0]} -> {raw['ts'].iloc[-1]}")

    is_lo, is_hi = SPLITS.get((symbol, tf), ("2026-06-11", "2026-10-30"))
    for hyg_name, hyg in (("default", None), ("strict", strict_hygiene)):
        b = raw if hyg is None else hyg(raw)
        is_b = slice_window(b, is_lo, is_hi)
        ho_b = slice_window(b, "2000-01-01", is_lo)
        print(f"\n== hygiene={hyg_name}: IS {len(is_b)} bars ({is_b['ts'].iloc[0]} -> "
              f"{is_b['ts'].iloc[-1]})   HOLDOUT {len(ho_b)} bars "
              f"({ho_b['ts'].iloc[0]} -> {ho_b['ts'].iloc[-1]})")
        if len(is_b) < 400 or len(ho_b) < 400:
            print("   not enough bars in one side -- skipping")
            continue

        # ---- 1) SEARCH on IS only (3-fold chronological WF inside the IS) ---
        n = len(is_b)
        folds = [(int(n * a), int(n * b)) for a, b in
                 ((0.40, 0.60), (0.60, 0.80), (0.80, 1.00))]
        cands = []
        for sess in SESSIONS:
            for dk in DISP:
                for rr in RR:
                    for body in BODY:
                        cfg = dict(BASE)
                        cfg.update(sess_lo=sess[0], sess_hi=sess[1], disp_k=dk,
                                   rr=rr, body_ratio_min=body)
                        sigs = sigs_sweep_disp(is_b, dict(cfg, _tf=tf))
                        pfs, ns = [], 0
                        for a, b in folds:
                            sub = is_b.iloc[a:b].reset_index(drop=True)
                            ss = {i - a: v for i, v in sigs.items() if a <= i < b}
                            r = simulate_prod(sub, ss, symbol)
                            pfs.append(r["pf"])
                            ns += r["n_trades"]
                        if ns < 20:
                            continue
                        cands.append({
                            "sess": sess, "disp_k": dk, "rr": rr, "body": body,
                            "min_pf": float(min(pfs)), "mean_pf": float(np.mean(pfs)),
                            "n": ns, "folds": [round(x, 3) for x in pfs],
                        })
        cands.sort(key=lambda c: (c["min_pf"], c["mean_pf"]), reverse=True)
        print(f"   IS search: {len(cands)} configs with n>=20; top 8 by (minPF, meanPF):")
        for c in cands[:8]:
            print(f"     ISminPF={c['min_pf']:.3f} ISmeanPF={c['mean_pf']:.3f} n={c['n']:4d} "
                  f"folds={c['folds']} sess={c['sess']} disp={c['disp_k']} rr={c['rr']} body={c['body']}")

        # ---- 2) SCORE the IS winners ONCE on the untouched HOLDOUT --------
        print("   HOLDOUT (scored once, never used for selection):")
        for c in cands[:8]:
            cfg = dict(BASE)
            cfg.update(sess_lo=c["sess"][0], sess_hi=c["sess"][1], disp_k=c["disp_k"],
                       rr=c["rr"], body_ratio_min=c["body"])
            r = score(ho_b, cfg, tf, symbol)
            sigs = sigs_sweep_disp(ho_b, dict(cfg, _tf=tf))
            pm = per_month(ho_b, sigs, symbol)
            print(f"     sess={str(c['sess']):9s} disp={c['disp_k']:<4} rr={c['rr']:<4} "
                  f"body={c['body']:<5} | HOLDOUT pf={r['pf']:.3f} n={r['n_trades']:3d} "
                  f"er={r['expectancy_r']:+.3f} pnl=${r['total_pnl']:.0f} "
                  f"months={[(m[0], m[1], round(m[2], 2)) for m in pm]}")


if __name__ == "__main__":
    main()
