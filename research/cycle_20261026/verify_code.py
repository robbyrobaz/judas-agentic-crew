"""Verify a RENDERED market-entry code against the screened configuration.

Runs the rendered file through the same honest harness used by the screen
(clean segments, LIVE fill = market at the next bar's open, IS/OOS split by
time) and prints the three books.  If base == fair == live the file has no
retro-fill premium and the house stamp can be trusted; if live differs from
base the file still names a structural entry and must not be proposed.

Usage:
  .venv/bin/python research/cycle_20261026/verify_code.py <code_file> <SYM> <tf>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE_FILE = Path(sys.argv[1])
SYMBOL = sys.argv[2].upper()
TF = sys.argv[3]

code = CODE_FILE.read_text()
segs = clean_segments(SYMBOL, TF)
if not segs:
    raise SystemExit(f"{SYMBOL} {TF}: no clean segment")
boundary = split_ts(SYMBOL, TF, segs)

per = {"live": [], "base": [], "fair": []}
n_sig = 0
for seg in segs:
    b, lv, fa, meta = run_books(code, seg, {}, SYMBOL)
    n_sig += meta.get("n_signals", 0)
    per["live"].append(lv)
    per["base"].append(b)
    per["fair"].append(fa)


def partition(kind):
    is_t, oos_t = [], []
    for seg, trades in zip(segs, per[kind]):
        for t in trades:
            ts = seg["ts"].iloc[t["entry_bar"]]
            (is_t if ts <= boundary else oos_t).append(t)
    return is_t, oos_t


out = {"symbol": SYMBOL, "tf": TF, "code_file": str(CODE_FILE),
       "n_segments": len(segs), "n_bars": int(sum(len(s) for s in segs)),
       "n_signals": n_sig, "boundary": str(boundary)}
for kind in ("live", "base", "fair"):
    is_t, oos_t = partition(kind)
    out[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                 "full": metrics(is_t + oos_t)}
print(json.dumps(out, indent=1, default=str))
