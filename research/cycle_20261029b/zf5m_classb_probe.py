"""ZF 5m class-B probe — task #2892 follow-up.

The archive-readiness audit reports ZF_5m as NOT_TESTABLE under the STRICT
mask (range>0 AND volume>0): zero contiguous clean runs >= 300 bars.  The v2
report claims 2451 contiguous bars under the CORRECTED mask (placeholder =
range==0 AND volume==0).  The two masks differ ONLY on class B: bars with
range==0 but volume>0.

Question: is a class-B bar (high == low, volume > 0) a REAL trade bar or a
data artefact?  For ZF (5-year Treasury note, tick = 1/32) a single-print bar
is physically plausible -- the instrument genuinely trades at one price for a
whole 5-minute bucket when nobody is bidding.

Decision rule used here: a class-B bar is REAL if
  (a) its OHLC are all identical (a single print, not a collapsed bar), AND
  (b) its volume is a plausible round trade count (>= 1), AND
  (c) it is NOT part of a long run of consecutive class-B bars (a run of
      class-B bars would be a stuck-feed artefact, not a thin market).

If class-B bars are real, the strict mask is WRONG for ZF and the cell is
screenable.  This file answers that question with data.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import bar_archive  # noqa: E402

SYMS = [("ZF", "5m"), ("ZF", "15m"), ("ZF", "1h"), ("6J", "5m"), ("MGC", "5m"),
        ("MNQ", "5m"), ("MCL", "5m"), ("MGC", "1h"), ("MCL", "15m"), ("MCL", "1h")]


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


def main() -> None:
    rows = []
    for sym, tf in SYMS:
        df = bar_archive.read_archive(sym, tf)
        if df.empty:
            continue
        rng = (df["high"] - df["low"]).to_numpy(float)
        vol = df["volume"].to_numpy(float)
        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)

        a_mask = (rng > 0) & (vol > 0)          # class A
        b_mask = (rng <= 0) & (vol > 0)         # class B: zero range, traded
        c_mask = (rng <= 0) & (vol <= 0)        # class C: true placeholder
        d_mask = (rng > 0) & (vol <= 0)         # class D: range but no volume

        # class B anatomy
        n_b = int(b_mask.sum())
        if n_b:
            bi = np.flatnonzero(b_mask)
            flat = (np.isclose(h[bi], l[bi]) & np.isclose(o[bi], h[bi])
                    & np.isclose(c[bi], h[bi]))
            bvol = vol[bi]
            # longest consecutive run of class B (row-adjacent)
            b_runs = runs_of(b_mask)
            max_b_run = max((b - a + 1) for a, b in b_runs)
            # class-B bars that are ADJACENT (row index) to a class-A bar
            adjacent = 0
            for i in bi:
                if (i > 0 and a_mask[i - 1]) or (i < len(a_mask) - 1 and a_mask[i + 1]):
                    adjacent += 1
        else:
            flat = np.array([], bool)
            bvol = np.array([])
            max_b_run = 0
            adjacent = 0

        # contiguous runs under each mask
        def longest(mask):
            rr = [(a, b) for a, b in runs_of(mask) if b - a + 1 >= 2]
            if not rr:
                return 0, None, None
            a, b = max(rr, key=lambda r: r[1] - r[0])
            seg = df.iloc[a:b + 1]
            return b - a + 1, str(seg["ts"].iloc[0]), str(seg["ts"].iloc[-1])

        strict = (rng > 0) & (vol > 0)
        fixed = ~((rng <= 0) & (vol <= 0))      # v2 corrected mask

        s_n, s_a, s_b = longest(strict)
        f_n, f_a, f_b = longest(fixed)

        rec = {
            "cell": f"{sym}_{tf}",
            "raw": len(df),
            "A": int(a_mask.sum()), "B": n_b, "C": int(c_mask.sum()), "D": int(d_mask.sum()),
            "B_pct": round(100.0 * n_b / len(df), 1),
            "B_all_ohlc_identical_pct": round(100.0 * float(flat.mean()), 1) if n_b else None,
            "B_max_consecutive_run": max_b_run,
            "B_adjacent_to_A_pct": round(100.0 * adjacent / n_b, 1) if n_b else None,
            "B_vol_median": float(np.median(bvol)) if n_b else None,
            "B_vol_max": float(np.max(bvol)) if n_b else None,
            "longest_STRICT": s_n, "longest_FIXED": f_n,
            "strict_first": s_a, "strict_last": s_b,
            "fixed_first": f_a, "fixed_last": f_b,
        }
        rows.append(rec)
        print(f"{rec['cell']:<9} raw={rec['raw']:<6} A={rec['A']:<6} B={rec['B']:<5}({rec['B_pct']}%) "
              f"C={rec['C']:<5} D={rec['D']:<4} | B identical OHLC={rec['B_all_ohlc_identical_pct']}% "
              f"maxB_run={rec['B_max_consecutive_run']:<4} B_adjA={rec['B_adjacent_to_A_pct']}% "
              f"Bvol med={rec['B_vol_median']} | STRICT={rec['longest_STRICT']:<6} FIXED={rec['longest_FIXED']}")

    out = Path("outputs/zf5m_classb_probe_20261029.json")
    out.write_text(json.dumps(rows, indent=2, default=str))

    # ---- what does ZF 5m look like around a class-B cluster? ----
    df = bar_archive.read_archive("ZF", "5m")
    rng = (df["high"] - df["low"]).to_numpy(float)
    vol = df["volume"].to_numpy(float)
    b_mask = (rng <= 0) & (vol > 0)
    print("\n--- ZF 5m sample rows (raw, first 25) ---")
    print(df.head(25).to_string())
    print("\n--- ZF 5m rows around first class-B bar ---")
    bi = np.flatnonzero(b_mask)
    if len(bi):
        i = int(bi[0])
        print(df.iloc[max(0, i - 4):i + 5].to_string())

    outp = Path("outputs/zf5m_classb_probe_20261029.json")
    outp.write_text(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    import json
    main()
