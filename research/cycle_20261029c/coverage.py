"""Archive coverage probe (segments, class-A/B/C split, span days) per cell."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261029b"))

from segment_honest_screen import segments  # noqa: E402


def main() -> int:
    rows = []
    for sym in ("MGC", "MNQ", "MCL", "ZF", "6J"):
        for tf in ("5m", "15m", "1h"):
            try:
                segs = segments(sym, tf, mask="fixed", min_seg=300)
            except Exception as e:  # pragma: no cover
                rows.append((sym, tf, "ERR", str(e)[:60], "", "", ""))
                continue
            if not segs:
                rows.append((sym, tf, 0, 0, 0, 0, "NO DATA"))
                continue
            n = sum(len(s) for s in segs)
            span = (segs[-1]["ts"].iloc[-1] - segs[0]["ts"].iloc[0]).days
            rows.append((sym, tf, len(segs), n, span,
                         max(len(s) for s in segs),
                         str(segs[0]["ts"].iloc[0])[:10]))
    print(f"{'sym':<5}{'tf':<5}{'segs':>6}{'bars':>9}{'span_d':>8}{'maxseg':>9}  first")
    for r in rows:
        print(f"{r[0]:<5}{r[1]:<5}{str(r[2]):>6}{str(r[3]):>9}{str(r[4]):>8}{str(r[5]):>9}  {r[6]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
