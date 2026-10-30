"""Archive/segment coverage probe for the 2026-10-29e cycle (task #2893).

Prints, per symbol/timeframe: total bars, clean segments (mask=fixed), bars per
segment, first/last clean bar, and the global 60% IS boundary -- so we know
which cells can carry an honest IS/OOS split and how wide the fit really is.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from segment_honest_screen import segments, is_oos_boundary  # noqa: E402

PAIRS = [(s, tf) for s in ("MGC", "MNQ", "MCL", "ZF", "6J")
         for tf in ("5m", "15m", "1h")]

if __name__ == "__main__":
    for sym, tf in PAIRS:
        for mask in ("fixed", "strict"):
            segs = segments(sym, tf, mask=mask)
            if not segs:
                print(f"{sym}_{tf:4s} {mask:7s} NO SEGMENTS")
                continue
            bars = [len(s) for s in segs]
            b = is_oos_boundary(segs)
            print(f"{sym}_{tf:4s} {mask:7s} segs={len(segs):2d} bars={sum(bars):6d} "
                  f"per_seg={bars} first={segs[0]['ts'].iloc[0]} "
                  f"last={segs[-1]['ts'].iloc[-1]} IS_boundary={b}")
