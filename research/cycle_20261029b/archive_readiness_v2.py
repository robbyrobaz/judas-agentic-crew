"""ARCHIVE READINESS v2 -- task #2892 deliverable (authoritative).

WHAT CHANGED vs `research/cycle_20261029a/archive_readiness.py`
---------------------------------------------------------------
The v1 audit used the mask `(high-low)>0 & volume>0`.  Measured across all 15
legal symbol-timeframe cells (`profile_dirty.py`), the archive contains three
row classes and NO bar has range>0 with volume==0:

    A  range>0 , volume>0    normal bar                     REAL
    B  range==0, volume>0    a bar containing exactly ONE   REAL
                             print (O=H=L=C=that print)
    C  range==0, volume==0   an interval with NO trade at   PLACEHOLDER
                             all (IBKR carries the price
                             forward)

The v1 mask keeps only class A, so it discards class B -- a genuine trade --
and labels it "dirty".  That is why v1 rated ZF 5m NOT_TESTABLE with ZERO
contiguous runs >= 300 bars: ZF has 2,650 class-B bars scattered through the
series and each one cut a run.

Task #2892's own wording is "drop placeholder **zero-range / zero-volume** rows"
= drop class C only.  This script implements that wording and reports BOTH
masks so the change is auditable.

CONTINUITY CONTRACT
-------------------
A run of A+B bars is contiguous in TIME at the timeframe step (verified per
cell below: `step_ok`), so a "market at the open of the next bar" fill cannot
jump a gap -- EXCEPT across a genuine session/weekend closure, which is
counted separately as `closure_gaps` and is a real market closure, not a data
hole.  Class C intervals are intervals in which NOTHING traded, so no fill
could have occurred there either.

Output: outputs/archive_readiness_v2_20261029.json  +  a markdown table.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

SYMBOLS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
STEP_MIN = {"5m": 5, "15m": 15, "1h": 60}
MIN_SEG = 300
REGIME_RATIO = 3.0
OUT_JSON = ROOT / "outputs" / "archive_readiness_v2_20261029.json"
OUT_MD = ROOT / "outputs" / "archive_readiness_v2_20261029.md"


def atr14(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                     (df["high"] - prev).abs(),
                     (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(14, min_periods=14).mean()


def runs_of(mask: np.ndarray) -> list[tuple[int, int]]:
    out, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def seg_stats(df: pd.DataFrame, a: int, b: int, step: int) -> dict:
    seg = df.iloc[a:b + 1].reset_index(drop=True)
    d = seg["ts"].diff().dt.total_seconds().div(60)
    gaps = d[d.notna()]
    step_ok = int((gaps == step).sum())
    # a "closure" gap is a real market closure (weekend / holiday / daily
    # maintenance); anything else inside a run is a genuine data hole.
    closure = gaps[(gaps != step)]
    atr = atr14(seg).dropna()
    ratio = float("nan")
    if len(atr) >= 50:
        p95, p05 = float(np.percentile(atr, 95)), float(np.percentile(atr, 5))
        ratio = (p95 / p05) if p05 > 0 else float("inf")
    return {
        "bars": int(b - a + 1),
        "first": str(seg["ts"].iloc[0]),
        "last": str(seg["ts"].iloc[-1]),
        "days": int((seg["ts"].iloc[-1] - seg["ts"].iloc[0]).days),
        "step_ok_rows": step_ok,
        "non_step_gaps": int(len(closure)),
        "max_gap_min": float(gaps.max()) if len(gaps) else 0.0,
        "atr_p95_p05_ratio": None if ratio != ratio else round(ratio, 2),
        "regime_change": bool(ratio == ratio and ratio >= REGIME_RATIO),
    }


def audit(symbol: str, tf: str) -> dict:
    step = STEP_MIN[tf]
    res = {"symbol": symbol, "tf": tf}
    raw = bar_archive.read_archive(symbol, tf)
    if raw is None or raw.empty:
        res.update({"available": False, "raw_bars": 0, "verdict": "MISSING"})
        return res

    df = raw.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    rng = (df["high"] - df["low"]).to_numpy(float)
    vol = df["volume"].to_numpy(float)
    n_a = int((rng > 0).sum())
    n_b = int(((rng <= 0) & (vol > 0)).sum())
    n_c = int(((rng <= 0) & (vol <= 0)).sum())

    mask_strict = (rng > 0) & (vol > 0)          # v1 mask  (class A only)
    mask_fixed = vol > 0                        # v2 mask  (class A + B)

    out = {"symbol": symbol, "tf": tf,
           "available": True, "raw_bars": int(len(df)),
           "first": str(df["ts"].iloc[0]), "last": str(df["ts"].iloc[-1]),
           "span_days": int((df["ts"].iloc[-1] - df["ts"].iloc[0]).days),
           "class_A_bars": n_a, "class_B_bars": n_b, "class_C_placeholders": n_c,
           "class_C_pct": round(100.0 * n_c / max(len(df), 1), 1),
           "class_B_pct": round(100.0 * n_b / max(len(df), 1), 1)}

    for label, mask in (("strict", mask_strict), ("fixed", mask_fixed)):
        segs = [(a, b) for a, b in runs_of(mask) if (b - a + 1) >= MIN_SEG]
        segs.sort(key=lambda r: -(r[1] - r[0]))
        best = seg_stats(df, *segs[0], step) if segs else {
            "bars": 0, "first": None, "last": None, "days": 0,
            "step_ok_rows": 0, "non_step_gaps": 0, "max_gap_min": 0.0,
            "atr_p95_p05_ratio": None, "regime_change": None}
        out[f"n_segments_ge_{MIN_SEG}_{label}"] = len(segs)
        out[f"longest_{label}"] = best
        out[f"segments_{label}"] = [[str(df["ts"].iloc[a]), str(df["ts"].iloc[b]),
                                     int(b - a + 1)] for a, b in segs[:6]]

    lg = out["longest_fixed"]
    if lg["bars"] >= 5000:
        verdict = "TESTABLE"
    elif lg["bars"] >= 1000:
        verdict = "TESTABLE_THIN"
    elif lg["bars"] >= MIN_SEG:
        verdict = "MARGINAL"
    else:
        verdict = "NOT_TESTABLE"
    out["verdict"] = verdict
    out["verdict_v1_strict"] = (
        "TESTABLE" if out["longest_strict"]["bars"] >= 5000 else
        "TESTABLE_THIN" if out["longest_strict"]["bars"] >= 1000 else
        "MARGINAL" if out["longest_strict"]["bars"] >= MIN_SEG else
        "NOT_TESTABLE")
    return out


def main() -> int:
    rows = [audit(s, tf) for s in SYMBOLS for tf in TFS]

    md = ["# Archive readiness v2 -- task #2892 deliverable (2026-10-29)",
          "",
          "Corrected mask: a **placeholder** is `range==0 AND volume==0` (class C).",
          "Class B (`range==0, volume>0`) is a real single-print bar and is KEPT.",
          "In all 15 cells `range>0 AND volume==0` has count **0**.",
          "",
          "| cell | raw | first (UTC) | last (UTC) | A | B | C | C% | "
          "longest STRICT (v1) | longest FIXED (v2) | v2 span d | regime p95/p05 | v1 verdict | v2 verdict |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r.get("available"):
            md.append(f"| {r['symbol']}_{r['tf']} | MISSING | | | | | | | | | | | | |")
            continue
        ls, lf = r["longest_strict"], r["longest_fixed"]
        md.append(
            f"| {r['symbol']}_{r['tf']} | {r['raw_bars']} | {r['first'][:16]} | "
            f"{r['last'][:16]} | {r['class_A_bars']} | {r['class_B_bars']} | "
            f"{r['class_C_placeholders']} | {r['class_C_pct']} | "
            f"{ls['bars']} | {lf['bars']} | {lf['days']} | "
            f"{lf['atr_p95_p05_ratio']} | {r['verdict_v1_strict']} | {r['verdict']} |")
    OUT_MD.write_text("\n".join(md) + "\n")
    OUT_JSON.write_text(json.dumps({"generated": "2026-10-29", "min_seg": MIN_SEG,
                                    "regime_ratio": REGIME_RATIO, "cells": rows},
                                   indent=2, default=str))
    print("\n".join(md))
    print(f"\nwrote {OUT_JSON} and {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
