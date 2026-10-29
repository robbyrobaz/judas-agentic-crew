"""SEGMENT-AWARE HONEST SCREEN on the CORRECTED archive mask (task #2892 follow-on).

WHY
---
`run_custom_backtest` produces dollar-stamped envelopes that are STRUCTURALLY
INVALID: finding 07f3a4c2 / #9126 showed the house engine resolves an entry
from a bar that had already CLOSED (retro-fill).  csid229 PF 5.044 -> 0.643 and
csid368 PF 6.325 -> 1.054 under a fair decision-time entry.  Coder autofix
#565 (the retro-fill guard) FAILED with `harness_no_fix`.  So a v1 stamp from
`run_custom_backtest` is NOT promotion-grade evidence and must not be proposed.

`research/cycle_20261025/honest_screen.py` is the researcher's own harness that
produces the missing evidence class:
  * `base` book  = house engine semantics (fill at the named level) -- NOT used
  * `live` book  = MARKET at the OPEN of the next PHYSICAL bar, brackets
                   re-anchored to the fill, gap-aware exits  <-- PRODUCTION
  * `fair` book  = market-on-close at the decision bar
  * costs via `src.tools.cost_model.apply_costs_to_trade`, COST_MODEL_VERSION
    = "v1_realistic_micros", dollar-denominated.

WHAT THIS FILE ADDS
-------------------
`honest_screen.load_bars` tails the RAW archive, so placeholder rows are fed
to `evaluate()` and the strategy's own internal filter (`high-low>0 &
volume>0`) silently DELETES them -- which means the strategy's "current bar"
can be many physical bars old and a fill can jump a placeholder hole.

This file screens PER CLEAN SEGMENT instead, so:
  * every bar in a segment is a real bar at a real timestamp,
  * the strategy's internal filter still runs (it drops class-B single-print
    bars, which is REQUIRED -- a zero-range bar fabricates a sweep),
  * the live fill is at the next physical bar's OPEN -- no time jump.

SEGMENT MASK (corrected, from archive_readiness_v2.py):
  * "fixed"  -> placeholder is `range==0 AND volume==0` (class C).  Class B
                (`range==0, volume>0`, a real single print) is KEPT.
  * "strict" -> the v1 mask `range>0 AND volume>0` (class A only).

Usage:
    .venv/bin/python research/cycle_20261029b/segment_honest_screen.py <json-spec>
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import metrics, run_books  # noqa: E402
from src import bar_archive  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

IS_FRAC = 0.60
MIN_SEG = 300

_SEG_CACHE: dict = {}


def segments(symbol: str, tf: str, mask: str = "fixed", min_seg: int = MIN_SEG):
    """Contiguous runs of real bars.  `fixed` keeps class-B single-print bars."""
    key = (symbol, tf, mask, min_seg)
    if key in _SEG_CACHE:
        return _SEG_CACHE[key]
    df = bar_archive.read_archive(str(symbol).upper(), tf)
    if df is None or df.empty:
        _SEG_CACHE[key] = []
        return []
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    rng = (df["high"] - df["low"]).to_numpy(float)
    vol = df["volume"].to_numpy(float)
    if mask == "strict":
        ok = (rng > 0) & (vol > 0)
    else:
        ok = vol > 0
    out, start = [], None
    idxs = np.arange(len(df))
    for i, v in enumerate(ok):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_seg:
                out.append(df.iloc[start:i].reset_index(drop=True))
            start = None
    if start is not None and len(df) - start >= min_seg:
        out.append(df.iloc[start:].reset_index(drop=True))
    _SEG_CACHE[key] = out
    return out


def is_oos_boundary(segs):
    if not segs:
        return None
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    return t0 + (t1 - t0) * IS_FRAC


def screen(code: str, symbol: str, tf: str, params: dict, qty: int = 1,
           mask: str = "fixed", min_seg: int = MIN_SEG, budget_s: float = 240.0):
    segs = segments(symbol, tf, mask=mask, min_seg=min_seg)
    if not segs:
        return {"error": "no segments", "symbol": symbol, "tf": tf, "mask": mask}
    boundary = is_oos_boundary(segs)

    live_all, base_all, fair_all = [], [], []
    meta = {}
    t0 = time.time()
    for si, seg in enumerate(segs):
        if time.time() - t0 > budget_s:
            meta["timed_out"] = True
            break
        b, lv, fa, m = run_books(code, seg, params, symbol, qty=qty,
                                 budget_s=max(30.0, budget_s / max(len(segs), 1)))
        meta = m
        # tag each trade with the segment and the physical ts of its entry bar
        for tr in (b, lv, fa):
            for t in tr:
                eb = t["entry_bar"]
                t["seg"] = si
                t["ts"] = str(seg["ts"].iloc[min(eb, len(seg) - 1)])
        live_all += lv
        base_all += b
        fair_all += fa

    def split(trades):
        is_t = [t for t in trades
                if boundary is None or pd.Timestamp(t["ts"]) <= boundary]
        oos_t = [t for t in trades
                 if boundary is not None and pd.Timestamp(t["ts"]) > boundary]
        return is_t, oos_t

    li, lo = split(live_all)
    bi, bo = split(base_all)
    fi, fo = split(fair_all)

    return {
        "symbol": symbol, "tf": tf, "mask": mask,
        "n_segments": len(segs),
        "segment_bars": [int(len(s)) for s in segs],
        "seg_first": str(segs[0]["ts"].iloc[0]), "seg_last": str(segs[-1]["ts"].iloc[-1]),
        "is_boundary": str(boundary) if boundary is not None else None,
        "cost_model": COST_MODEL_VERSION, "qty": qty,
        "params": params,
        "live": metrics(live_all), "live_IS": metrics(li), "live_OOS": metrics(lo),
        "fair": metrics(fair_all),
        "base": metrics(base_all), "base_IS": metrics(bi), "base_OOS": metrics(bo),
        "meta": meta,
        "_live_trades": live_all,
    }


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[1]).read_text())
    out = []
    for job in spec:
        code = Path(job["code_file"]).read_text()
        r = screen(code, job["symbol"], job["tf"], job.get("params", {}),
                   qty=job.get("qty", 1), mask=job.get("mask", "fixed"))
        r.pop("_live_trades", None)
        out.append(r)
        print(json.dumps(r, indent=2, default=str))
    Path(sys.argv[2]).write_text(json.dumps(out, indent=2, default=str))
