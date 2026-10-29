"""Reconcile the two clean-segment definitions and map the REAL trading data.

Definition A (task text): drop placeholder rows, then the clean segment is the
  remaining rows over a continuous date range.
Definition B (strict): longest run of adjacent-in-time clean bars (any
  placeholder OR hole breaks it).

Also: gap census inside the real-data region so we can see weekends vs real
holes vs contract rolls.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import bar_archive  # noqa: E402

BAR_MIN = {"5m": 5, "15m": 15, "1h": 60}


def analyse(sym, tf):
    df = bar_archive.read_archive(sym, tf)
    step = pd.Timedelta(minutes=BAR_MIN[tf])
    hi, lo, vol = df["high"].to_numpy(float), df["low"].to_numpy(float), df["volume"].to_numpy(float)
    clean = (hi > lo) & (vol > 0)
    cdf = df[clean].reset_index(drop=True)
    print(f"\n{'='*78}\n{sym} {tf}: raw={len(df)} clean={len(cdf)} placeholders={int((~clean).sum())}")
    if cdf.empty:
        return
    d = cdf["ts"].diff()
    gaps = d[d > step]
    # gap census by bucket
    buckets = {"<=15min": 0, "15m-2h": 0, "2h-24h": 0, "24h-49h(wknd)": 0, ">49h": 0}
    for g in gaps:
        m = g.total_seconds() / 60.0
        if m <= 15: buckets["<=15min"] += 1
        elif m <= 120: buckets["15m-2h"] += 1
        elif m <= 1440: buckets["2h-24h"] += 1
        elif m <= 2940: buckets["24h-49h(wknd)"] += 1
        else: buckets[">49h"] += 1
    print(f"  clean span: {cdf['ts'].iloc[0]} -> {cdf['ts'].iloc[-1]}  ({(cdf['ts'].iloc[-1]-cdf['ts'].iloc[0]).days}d)")
    print(f"  gaps in clean series: {len(gaps)}  {buckets}")
    if len(gaps):
        big = gaps.sort_values(ascending=False).head(6)
        for g in big:
            i = cdf["ts"].diff().eq(g).idxmax()
            print(f"    hole {g.total_seconds()/3600:8.1f}h before {cdf['ts'].iloc[i]}")

    # Definition A: drop placeholders; segment = maximal span w/ no hole > 49h
    big_break = d > pd.Timedelta(hours=49)
    seg_id = big_break.cumsum()
    segs = []
    for sid, g in cdf.groupby(seg_id):
        segs.append({"first": str(g["ts"].iloc[0]), "last": str(g["ts"].iloc[-1]),
                     "n": len(g), "days": round((g["ts"].iloc[-1] - g["ts"].iloc[0]).days, 1)})
    segs.sort(key=lambda s: -s["n"])
    print(f"  DEF-A top segments (49h break): {segs[:4]}")

    # Definition B: strictly adjacent clean bars
    ts = df["ts"].reset_index(drop=True)
    dts = ts.diff().dt.total_seconds().div(60).to_numpy()
    gapb = dts > (BAR_MIN[tf] * 1.5)
    prevbad = np.concatenate([[False], (~clean)[:-1]])
    brk = gapb | prevbad
    sid = np.cumsum(brk)
    best = 0
    for s in range(1, int(sid.max()) + 1):
        idx = np.flatnonzero(sid == s)
        best = max(best, len(idx))
    print(f"  DEF-B longest strictly-adjacent clean run: {best} bars")
    return segs


for sym, tf in [("MGC", "5m"), ("MGC", "15m"), ("MGC", "1h"),
                ("MNQ", "5m"), ("MNQ", "15m"), ("MNQ", "1h"),
                ("MCL", "5m"), ("MCL", "15m"), ("MCL", "1h"),
                ("ZF", "5m"), ("ZF", "15m"), ("ZF", "1h"),
                ("6J", "5m"), ("6J", "15m"), ("6J", "1h")]:
    analyse(sym, tf)
