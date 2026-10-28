"""ARCHITECTURE SCREEN -- honest (live-fill) grid over the DEEP archives.

WHY THIS FILE
-------------
`focus_screen.py` (cycle_20261027) screened only the MGC winner's
neighbourhood, on the clean segments of the archive.  Two gaps remain:

  1. the `disp_pullback` family found on MCL 15m was LONG-ONLY (the template
     branch skips bearish displacement bars), so the family was measured on
     one side of the market;
  2. the IS/OOS split was 60/40 INSIDE the clean span -- a selection-biased
     split, because the same window produced the winner.

This script screens a grid of cells across EVERY legal symbol and BOTH fast
timeframes on the FULL deep archive (clean segments, so the "market at the
next bar open" fill can never jump a time gap), and splits IS/OOS BY TIME at
a boundary the grid never saw.

Fills: base (house) / fair (market-on-close) / live (market at next bar OPEN,
brackets re-anchored, gap-aware exits).  `live` is production semantics
(finding #9134) and is what we rank on.  `base == fair == live` is the
signature of an architecture with no retro-fill premium.

Usage:
  .venv/bin/python research/cycle_20261027/arch_screen.py <SYM> <tf> <codefile> <gridjson>
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

IS_FRAC = 0.60
MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15

_CODE_CACHE: dict = {}


def _code(path: str) -> str:
    if path not in _CODE_CACHE:
        _CODE_CACHE[path] = (ROOT / path).read_text()
    return _CODE_CACHE[path]


def _job(args):
    symbol, tf, code_path, params = args
    code = _code(code_path)
    segs = clean_segments(symbol, tf)
    if not segs:
        return None
    boundary = split_ts(symbol, tf, segs)
    per = {"base": [], "live": [], "fair": []}
    for seg in segs:
        b, lv, fa, meta = run_books(code, seg, params, symbol)
        if meta.get("load_error"):
            return None
        per["base"].append(b)
        per["live"].append(lv)
        per["fair"].append(fa)

    def partition(kind):
        is_t, oos_t = [], []
        for seg, trades in zip(segs, per[kind]):
            for t in trades:
                ts = seg["ts"].iloc[t["entry_bar"]]
                (is_t if ts <= boundary else oos_t).append(t)
        return is_t, oos_t

    res = {"params": params, "n_seg": len(segs),
           "n_bars": int(sum(len(s) for s in segs)),
           "boundary": str(boundary)}
    for kind in ("live", "base", "fair"):
        is_t, oos_t = partition(kind)
        res[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                     "full": metrics(is_t + oos_t)}
    return res


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    code_path = sys.argv[3]
    grid = json.loads(Path(sys.argv[4]).read_text())
    out_path = Path(sys.argv[5]) if len(sys.argv) > 5 else None

    jobs = [(symbol, tf, code_path, c) for c in grid]
    rows = []
    with ProcessPoolExecutor(max_workers=min(12, os.cpu_count() or 4)) as ex:
        for r in ex.map(_job, jobs):
            if r:
                rows.append(r)
    if not rows:
        print("no rows")
        return 1

    rows.sort(key=lambda r: -min(r["live"]["is"]["pf"], r["live"]["oos"]["pf"]))
    print(f"=== {symbol} {tf}  {code_path}  "
          f"bars={rows[0]['n_bars']} boundary={rows[0]['boundary']} ===")
    print(f"{'params':<74} {'IS n':>5} {'pf':>6} | {'OOS n':>5} {'pf':>6} "
          f"| {'full pf':>7}  verdict")
    surv = []
    for r in rows:
        i, o = r["live"]["is"], r["live"]["oos"]
        ok = (i["pf"] > 1.0 and o["pf"] > 1.0
              and i["n"] >= MIN_TRADES_IS and o["n"] >= MIN_TRADES_OOS)
        if ok:
            surv.append(r)
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "setup")})
        print(f"{tag:<74} {i['n']:>5} {i['pf']:>6.3f} | {o['n']:>5} "
              f"{o['pf']:>6.3f} | {r['live']['full']['pf']:>7.3f}"
              f"  {'SURVIVOR' if ok else ''}")
    print(f"\n=== {len(surv)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in surv:
        print(f"  {json.dumps(r['params'])}")
        print(f"      IS  live {json.dumps(r['live']['is'])}")
        print(f"      OOS live {json.dumps(r['live']['oos'])}")
        print(f"      FULL live {json.dumps(r['live']['full'])}")

    if out_path:
        Path(out_path).write_text(json.dumps(
            {"symbol": symbol, "tf": tf, "code": code_path,
             "n_bars": rows[0]["n_bars"], "boundary": rows[0]["boundary"],
             "rows": rows, "n_survivors": len(surv)}, indent=2, default=str))
        print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
