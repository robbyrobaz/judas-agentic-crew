"""Archive data-quality scan (2026-10-26).

The 2025-12 probe of the rolled MGC 15m history found a chunk with 70% of
bars having a ZERO range and only 39% of bars carrying volume -- a degraded
feed.  If that chunk sits inside a holdout window the holdout number is
meaningless, so every archive is profiled per month before it is used as
evidence.

Usage:  .venv/bin/python research/cycle_20261026/quality_scan.py [symbol ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

SYMS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["15m", "5m"]

out = {}
for sym in (sys.argv[1:] or SYMS):
    for tf in TFS:
        df = bar_archive.read_archive(sym, tf)
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        df["ts"] = pd.to_datetime(df["ts"])
        df["month"] = df["ts"].dt.strftime("%Y-%m")
        rng = (df["high"] - df["low"])
        df["zero"] = rng <= 0
        has_vol = "volume" in df.columns
        rows = []
        for m, g in df.groupby("month"):
            rows.append({
                "month": m,
                "n": int(len(g)),
                "zero_range_pct": round(100.0 * g["zero"].mean(), 1),
                "vol_gt0_pct": (round(100.0 * (g["volume"] > 0).mean(), 1)
                                if has_vol else None),
                "med_range": round(float(rng[g.index].median()), 6),
            })
        out[f"{sym}_{tf}"] = rows
        bad = [r for r in rows if r["zero_range_pct"] > 5]
        print(f"--- {sym} {tf}: {len(df)} bars, {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
        for r in rows:
            flag = "  <== DEGRADED" if r["zero_range_pct"] > 5 else ""
            print(f"    {r['month']}  n={r['n']:>5}  zero%={r['zero_range_pct']:>5}  "
                  f"vol%={r['vol_gt0_pct']}  med_rng={r['med_range']}{flag}")
        if bad:
            print(f"    !! {len(bad)} degraded month(s): "
                  f"{[r['month'] for r in bad]}")

p = ROOT / "outputs" / "archive_quality_20261026.json"
p.write_text(json.dumps(out, indent=1))
print(f"\nwrote {p}")
