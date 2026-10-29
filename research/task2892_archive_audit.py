"""Task #2892 — archive readiness audit for every legal symbol/timeframe cell.

Reports, per cell:
  * raw bar count (after ts dedupe, as read_archive does)
  * first / last bar timestamp (UTC)
  * TRUE CONTIGUOUS CLEAN-SEGMENT length: the longest run of consecutive
    bars that are non-placeholder (high > low AND volume > 0), measured on
    the *timeline* (gap-aware), not on the row index.
  * the clean segment's first/last ts and calendar span
  * total placeholder rows dropped inside the maths
  * whether the clean segment spans a regime change (proxy: the segment
    contains a >25% rolling-20 ATR percentile swing on MGC, or for
    non-MGC cells a >=15% realized-range expansion event inside it)
  * gaps larger than 1 expected bar interval (coverage holes)

Also emits an archive_readiness table (JSON + markdown) so the team knows
exactly which cells are testable from now on.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bar_archive  # noqa: E402

LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
BAR_MIN = {"5m": 5, "15m": 15, "1h": 60}

OUT_JSON = Path("outputs/archive_readiness.json")
OUT_MD = Path("outputs/archive_readiness.md")


def clean_segments(df: pd.DataFrame, tf: str) -> tuple[list[dict], int]:
    """Longest contiguous run of clean (non-placeholder) bars, gap-aware.

    A bar is clean when high > low AND volume > 0.  A "contiguous" run
    requires consecutive clean bars with no timeline gap bigger than 1.5x
    the expected bar interval (a missing bar is a coverage hole, not a
    clean run).
    """
    if df.empty:
        return [], 0
    step = pd.Timedelta(minutes=BAR_MIN[tf])
    ts = df["ts"].reset_index(drop=True)
    hi = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    ok = (hi > lo) & (vol > 0)
    n_placeholder = int((~ok).sum())

    tsv = ts.to_numpy()
    # break flag: a gap in the timeline larger than 1.5 bar intervals
    deltas = np.diff(tsv).astype("timedelta64[ns]").astype(np.int64)
    gap_break = deltas > (step.value * 1.5)
    # break when the previous bar was placeholder (so runs don't stitch across junk)
    prev_placeholder = ~ok[:-1]

    breaks = np.concatenate([[False], gap_break | prev_placeholder])
    seg_id = np.cumsum(breaks)
    best: list[dict] = []
    for sid in np.unique(seg_id):
        idx = np.flatnonzero(seg_id == sid)
        if len(idx) < 2:
            continue
        a, b = idx[0], idx[-1]
        # interior must be all-clean
        if not ok[a:b + 1].all():
            continue
        best.append({
            "start": str(ts.iloc[a]),
            "end": str(ts.iloc[b]),
            "n_bars": int(b - a + 1),
            "span_days": round((ts.iloc[b] - ts.iloc[a]).total_seconds() / 86400.0, 2),
        })
    best.sort(key=lambda d: -d["n_bars"])
    return best, n_placeholder


def holes(df: pd.DataFrame, tf: str) -> dict:
    if len(df) < 2:
        return {"n_gaps": 0, "max_gap_min": 0.0, "coverage_pct": 100.0}
    step = pd.Timedelta(minutes=BAR_MIN[tf])
    d = df["ts"].diff().dropna()
    gaps = d[d > step * 1.5]
    expected = df["ts"].iloc[-1] - df["ts"].iloc[0]
    n_exp = int(expected / step) + 1
    return {
        "n_gaps": int(len(gaps)),
        "max_gap_min": round(float(gaps.max().total_seconds() / 60.0), 1) if len(gaps) else 0.0,
        "coverage_pct": round(100.0 * len(df) / max(n_exp, 1), 2),
    }


def regime_change(seg_df: pd.DataFrame) -> dict:
    """Detect a regime change inside the clean segment.

    Proxy: rolling 20-bar realized range (ATR-like), then look for the
    segment's ATR distribution. A 'regime span' means the segment contains
    a period whose rolling-20 ATR is >=2.0x the segment median ATR
    (expansion) AND a period at <=0.6x (compression) -- i.e. the window
    genuinely covers more than one volatility regime.
    """
    if len(seg_df) < 60:
        return {"regime_span": False, "reason": "segment too short", "atr_ratio_max": None}
    h = seg_df["high"].to_numpy(float)
    l = seg_df["low"].to_numpy(float)
    c = seg_df["close"].to_numpy(float)
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    w = 20
    atr = pd.Series(tr).rolling(w).mean().dropna()
    if atr.empty:
        return {"regime_span": False, "reason": "no atr", "atr_ratio_max": None}
    med = float(atr.median())
    ratio_max = float(atr.max() / med) if med > 0 else None
    ratio_min = float(atr.min() / med) if med > 0 else None
    span = bool(ratio_max and ratio_max >= 2.0 and ratio_min and ratio_min <= 0.8)
    return {
        "regime_span": span,
        "reason": "atr expansion>=2.0x median AND compression<=0.8x median" if span else "single regime",
        "atr_ratio_max": round(ratio_max, 2) if ratio_max else None,
        "atr_ratio_min": round(ratio_min, 2) if ratio_min else None,
        "atr_median": round(med, 5),
    }


def main() -> None:
    rows = []
    for sym in LEGAL:
        for tf in TFS:
            p = bar_archive.archive_path(sym, tf)
            df = bar_archive.read_archive(sym, tf)
            rec: dict = {
                "symbol": sym, "timeframe": tf, "exists": p.exists(),
                "path": str(p),
                "file_bytes": p.stat().st_size if p.exists() else 0,
                "mtime": pd.Timestamp(p.stat().st_mtime, unit="s", tz="UTC").isoformat() if p.exists() else None,
                "raw_bars": int(len(df)),
            }
            if df.empty:
                rec.update({
                    "first": None, "last": None, "span_days": 0,
                    "clean_seg_bars": 0, "clean_seg_first": None, "clean_seg_last": None,
                    "clean_seg_span_days": 0, "placeholder_rows": 0,
                    "n_clean_segments": 0, "regime_span": False,
                    "testable": False, "note": "EMPTY ARCHIVE",
                })
                rows.append(rec)
                continue
            segs, n_ph = clean_segments(df, tf)
            top = segs[0] if segs else {"start": None, "end": None, "n_bars": 0, "span_days": 0}
            # clean segment df for regime test
            seg_df = df.iloc[0:0]
            if segs:
                mask = (df["ts"] >= pd.Timestamp(top["start"])) & (df["ts"] <= pd.Timestamp(top["end"]))
                seg_df = df[mask]
            rg = regime_change(seg_df)
            h = holes(df, tf)
            span_days = int((df["ts"].iloc[-1] - df["ts"].iloc[0]).days)
            rec.update({
                "first": str(df["ts"].iloc[0]),
                "last": str(df["ts"].iloc[-1]),
                "span_days": span_days,
                "clean_seg_bars": top["n_bars"],
                "clean_seg_first": top["start"],
                "clean_seg_last": top["end"],
                "clean_seg_span_days": top["span_days"],
                "placeholder_rows": n_ph,
                "n_clean_segments": len(segs),
                "second_seg_bars": segs[1]["n_bars"] if len(segs) > 1 else 0,
                "n_gaps": h["n_gaps"], "max_gap_min": h["max_gap_min"],
                "coverage_pct": h["coverage_pct"],
                "regime_span": rg["regime_span"], "regime_reason": rg["reason"],
                "atr_ratio_max": rg.get("atr_ratio_max"),
                "atr_ratio_min": rg.get("atr_ratio_min"),
                "testable": bool(top["n_bars"] >= 500),
            })
            rows.append(rec)
            print(json.dumps({k: rec[k] for k in (
                "symbol", "timeframe", "raw_bars", "first", "last", "clean_seg_bars",
                "clean_seg_first", "clean_seg_last", "clean_seg_span_days",
                "placeholder_rows", "n_clean_segments", "n_gaps", "max_gap_min",
                "coverage_pct", "regime_span", "testable")}, indent=2), flush=True)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rows, indent=2))

    md = ["# Archive readiness — task #2892", "",
          f"_generated {pd.Timestamp.utcnow().isoformat()}_", "",
          "| symbol | tf | raw bars | first (UTC) | last (UTC) | clean seg bars | clean seg span (d) | placeholders | gaps | cover% | regime span | testable |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append("| {symbol} | {timeframe} | {raw_bars} | {first} | {last} | {clean_seg_bars} | {clean_seg_span_days} | {placeholder_rows} | {n_gaps} | {coverage_pct} | {regime_span} | {testable} |".format(**r))
    OUT_MD.write_text("\n".join(md) + "\n")
    print("\nWROTE", OUT_JSON, OUT_MD)


if __name__ == "__main__":
    main()
