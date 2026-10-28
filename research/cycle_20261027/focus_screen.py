"""FOCUSED honest screen for the MARKET-ENTRY sweep->displacement family.

WHY THIS FILE
-------------
`seg_sweep.py` (2026-10-26/27) ran a 582-cell grid per (symbol, tf) and found
survivors on MGC 15m (setup=sweep_cont) and MCL 15m (setup=disp_pullback).
MNQ 15m had ZERO survivors and the golden-FVG family had ZERO survivors on
MGC 15m.  Those winners are the first cells in this book whose PF survives an
honest production fill (LIVE = market at the NEXT bar's open, brackets
re-anchored), and whose base == fair == live (no retro-fill premium).

This script re-runs a SMALL, EXPLICIT list of cells (the winner + its
neighbourhood) across every legal symbol and both fast timeframes, so that:

  1. the winner's neighbourhood is checked for a PLATEAU (a lone spike in the
     grid is a selection artefact, a plateau is a family),
  2. the winner is checked for CROSS-SYMBOL TRANSFER,
  3. every number is produced by the same LIVE-fill book, split IS/OOS BY
     TIME over the global clean span (first 60% IS).

No registry writes, no account writes.

Usage:
  .venv/bin/python research/cycle_20261027/focus_screen.py MGC 15m
  .venv/bin/python research/cycle_20261027/focus_screen.py --all
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

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261025" / "strategies" /
        "market_entry_ict_v1.py").read_text()

IS_FRAC = 0.60
MIN_TRADES_IS = 15
MIN_TRADES_OOS = 10

SYMBOLS = ["MGC", "MNQ", "MCL", "ZF", "6J"]


def cells() -> list[dict]:
    """The winner + its neighbourhood (+ the MCL disp_pullback family)."""
    out: list[dict] = []
    base = {"atr_period": 14, "stop_buf_atr": 0.15, "confirm_bars": 6,
            "body_ratio_min": 0.55}
    # sweep_cont neighbourhood around MGC's winner (lb=8, ms=0.45, disp=1.0, rr=1.5)
    for lb, ms, dk, rr in [
        (8, 0.45, 1.0, 1.5),    # <- the primary winner
        (8, 0.45, 1.2, 1.5),
        (12, 0.45, 1.0, 1.5),
        (8, 0.45, 0.8, 1.5),
        (6, 0.45, 1.0, 1.5),
        (20, 0.45, 1.0, 1.5),
        (8, 0.30, 1.0, 1.5),
        (8, 0.60, 1.0, 1.5),
        (12, 0.30, 1.0, 1.5),
        (32, 0.30, 1.0, 1.5),
        (8, 0.45, 1.0, 1.0),
        (8, 0.45, 1.0, 2.0),
    ]:
        p = dict(base)
        p.update({"setup": "sweep_cont", "lookback": lb,
                  "min_sweep_atr": ms, "disp_k": dk, "rr": rr})
        out.append(p)
    # the MCL 15m family (disp_pullback; NOTE: 'lookback' is unused by that
    # branch of the template -- only pb/retrace/disp_k/body/rr matter)
    for pb, rc, rr in [(3, 0.3, 2.0), (5, 0.3, 2.0), (5, 0.5, 1.0),
                        (8, 0.5, 1.0), (8, 0.3, 2.0)]:
        p = dict(base)
        p.update({"setup": "disp_pullback", "lookback": 12,
                  "pullback_bars": pb, "retrace": rc, "rr": rr, "disp_k": 1.0})
        out.append(p)
    return out


def _job(args):
    symbol, tf, params = args
    segs = clean_segments(symbol, tf)
    if not segs:
        return None
    boundary = split_ts(symbol, tf, segs)
    per = {"base": [], "live": [], "fair": []}
    for seg in segs:
        b, lv, fa, meta = run_books(CODE, seg, params, symbol)
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
           "n_bars": int(sum(len(s) for s in segs))}
    for kind in ("live", "base", "fair"):
        is_t, oos_t = partition(kind)
        res[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                     "full": metrics(is_t + oos_t)}
    return res


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--all":
        specs = [(s, tf) for s in SYMBOLS for tf in ("15m", "5m")]
    else:
        specs = [(args[0].upper(), args[1])]

    all_out = []
    for symbol, tf in specs:
        segs = clean_segments(symbol, tf)
        if not segs:
            print(f"### {symbol} {tf}: no clean segment")
            continue
        boundary = split_ts(symbol, tf, segs)
        print(f"### {symbol} {tf}: {len(segs)} segs, "
              f"{sum(len(s) for s in segs)} bars, boundary {boundary}")
        cs = cells()
        rows = []
        with ProcessPoolExecutor(
                max_workers=int(os.environ.get("SEG_WORKERS", "5"))) as ex:
            for r in ex.map(_job, [(symbol, tf, c) for c in cs], chunksize=1):
                if r:
                    rows.append(r)
        rows.sort(key=lambda r: -(r["live"]["full"]["pf"] or 0))
        for r in rows:
            p = {k: v for k, v in r["params"].items()
                 if k not in ("atr_period", "confirm_bars", "stop_buf_atr",
                              "body_ratio_min")}
            L, B, F = r["live"], r["base"], r["fair"]
            flag = "  <== SURVIVOR" if (L["is"]["pf"] > 1.0 and L["oos"]["pf"] > 1.0
                                        and L["is"]["n"] >= MIN_TRADES_IS
                                        and L["oos"]["n"] >= MIN_TRADES_OOS) else ""
            retro = round(B["full"]["pf"] - L["full"]["pf"], 3)
            print(f"  {json.dumps(p)}")
            print(f"      IS  pf={L['is']['pf']} n={L['is']['n']} "
                  f"${L['is']['total_usd']} | OOS pf={L['oos']['pf']} "
                  f"n={L['oos']['n']} ${L['oos']['total_usd']} | "
                  f"FULL pf={L['full']['pf']} n={L['full']['n']} "
                  f"${L['full']['total_usd']} dd=${L['full']['max_dd']} "
                  f"| base-fair-live retro={retro}{flag}")
        out = ROOT / "outputs" / f"focus_{symbol}_{tf}_20261027.json"
        out.write_text(json.dumps(
            {"symbol": symbol, "tf": tf, "n_segments": len(segs),
             "n_bars": int(sum(len(s) for s in segs)),
             "boundary": str(boundary), "rows": rows}, indent=1, default=str))
        print(f"  -> {out}")
        all_out.append({"symbol": symbol, "tf": tf, "rows": rows})
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
