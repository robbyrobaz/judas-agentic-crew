"""HONEST LIVE-BOOK SCREEN -- sweep family v4 (2026-10-28e, second pass).

Same machinery as screen_v3.py (clean segments, base/fair/live books, IS/OOS
time split, geometry guard).  Two grids:

  "sb"    the NEW "sb_day" setup -- ICT Silver Bullet at the previous session
          day's extremes (YT:r5rRJ0GKxA0) with the structure-shift (MSS) and
          volume confirmation switches.
  "cont4" the "cont" setup with the v4 switches (mss_mode, vol_k, body filter)
          on top of the MGC 5m lb40 base cell.

usage: .venv/bin/python research/cycle_20261028e/screen_v4.py <SYMBOL> <tf> [grid]
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261028e" / "strategies" /
        "sweep_sb_v4.py").read_text()

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15

BASE = {
    "setup": "cont",
    "lookback": 40,
    "ext_window": 0,
    "prom_atr": 0.0,
    "prom_win": 20,
    "trend_mode": 0,
    "pivot_len": 2,
    "mit50": 0,
    "cluster_atr": 0.15,
    "min_pivots": 2,
    "cluster_age": 40,
    "min_sweep_atr": 0.60,
    "disp_k": 1.0,
    "disp_mode": "through",
    "body_ratio_min": 0.55,
    "rr": 2.0,
    "confirm_bars": 6,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "hist_bars": 320,
    "sess_mode": 0,
    "sweep_body_max": 1.0,
    "target_mode": 0,
    "tgt_window": 60,
    "min_rr": 0.5,
    "mss_mode": 0,
    "mss_pl": 2,
    "vol_k": 0.0,
    "vol_win": 100,
    "day_window": 1500,
    "min_day_bars": 60,
}


def _c(**kw):
    d = dict(BASE)
    d.update(kw)
    return d


def grid_sb():
    """The new Silver-Bullet-at-the-day's-extremes setup."""
    cells = []
    for cb in (12, 24, 48):
        for ms in (0.30, 0.60):
            cells.append((f"cb{cb}_ms{int(ms*100):03d}", _c(
                setup="sb_day", confirm_bars=cb, min_sweep_atr=ms)))
    for cb in (12, 24, 48):
        cells.append((f"cb{cb}_mss", _c(setup="sb_day", confirm_bars=cb,
                                       min_sweep_atr=0.30, mss_mode=1)))
    for rr in (1.5, 3.0):
        cells.append((f"cb24_ms030_rr{int(rr*10):02d}", _c(
            setup="sb_day", confirm_bars=24, min_sweep_atr=0.30, rr=rr)))
    for dk in (0.8, 1.4):
        cells.append((f"cb24_ms030_dk{int(dk*10):02d}", _c(
            setup="sb_day", confirm_bars=24, min_sweep_atr=0.30, disp_k=dk)))
    for br in (0.35, 0.70):
        cells.append((f"cb24_ms030_br{int(br*100):03d}", _c(
            setup="sb_day", confirm_bars=24, min_sweep_atr=0.30, body_ratio_min=br)))
    for s in (3, 5):
        cells.append((f"cb24_ms030_sess{s}", _c(
            setup="sb_day", confirm_bars=24, min_sweep_atr=0.30, sess_mode=s)))
    for vk in (1.5, 2.5):
        cells.append((f"cb24_ms030_vol{int(vk*10):02d}", _c(
            setup="sb_day", confirm_bars=24, min_sweep_atr=0.30, vol_k=vk)))
    cells.append(("cb24_ms030_sb060", _c(setup="sb_day", confirm_bars=24,
                                         min_sweep_atr=0.30, sweep_body_max=0.60)))
    cells.append(("cb24_ms030_mss_vol20", _c(setup="sb_day", confirm_bars=24,
                                             min_sweep_atr=0.30, mss_mode=1,
                                             vol_k=2.0)))
    cells.append(("cb24_ms030_mdb20", _c(setup="sb_day", confirm_bars=24,
                                         min_sweep_atr=0.30, min_day_bars=20)))
    return cells


def grid_cont4():
    """The continuation setup with the v4 switches on the MGC 5m base cell."""
    cells = [("base", _c())]
    for pl in (1, 3):
        cells.append((f"mss_pl{pl}", _c(mss_mode=1, mss_pl=pl)))
    for vk in (1.5, 2.5, 4.0):
        cells.append((f"vol{int(vk*10):02d}", _c(vol_k=vk)))
    for b in (0.60, 0.40):
        cells.append((f"sbody{int(b*100):03d}", _c(sweep_body_max=b)))
    cells.append(("sbody060_mss", _c(sweep_body_max=0.60, mss_mode=1)))
    cells.append(("sbody060_vol20", _c(sweep_body_max=0.60, vol_k=2.0)))
    cells.append(("mss_sb060", _c(mss_mode=1, sweep_body_max=0.60)))
    cells.append(("mss_vol20", _c(mss_mode=1, vol_k=2.0)))
    for cb in (4, 10, 12):
        cells.append((f"cb{cb}", _c(confirm_bars=cb)))
    for lb in (30, 50, 60):
        cells.append((f"lb{lb}", _c(lookback=lb)))
    return cells


GRIDS = {"sb": grid_sb, "cont4": grid_cont4}


def _job(args):
    tag, symbol, tf, params = args
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

    res = {"tag": tag, "params": params, "n_seg": len(segs),
           "n_bars": int(sum(len(s) for s in segs))}
    for kind in ("live", "base", "fair"):
        is_t, oos_t = partition(kind)
        res[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                     "full": metrics(is_t + oos_t)}
    return res


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    grid_name = sys.argv[3] if len(sys.argv) > 3 else "sb"
    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: no clean segment")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}", flush=True)

    cells = GRIDS[grid_name]()
    print(f"  grid {grid_name}: {len(cells)} cells", flush=True)

    rows = []
    workers = int(os.environ.get("SEG_WORKERS", "6"))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_job, [(tag, symbol, tf, p) for tag, p in cells],
                        chunksize=1):
            if r:
                rows.append(r)
                print(f"  done {r['tag']} ({time.time() - t0:.0f}s)", flush=True)

    rows.sort(key=lambda r: -(r["live"]["is"]["pf"] if r["live"]["is"]["n"] >= MIN_TRADES_IS else -1))

    survivors = []
    for r in rows:
        il, ol = r["live"]["is"], r["live"]["oos"]
        if (il["pf"] > 1.0 and ol["pf"] > 1.0 and il["n"] >= MIN_TRADES_IS
                and ol["n"] >= MIN_TRADES_OOS):
            survivors.append(r)

    print("\n=== ALL CELLS (LIVE book) ===")
    for r in rows:
        il, ol, fl = r["live"]["is"], r["live"]["oos"], r["live"]["full"]
        print(f"  {r['tag']:22s} IS pf={il['pf']:.3f} n={il['n']:<4d} "
              f"OOS pf={ol['pf']:.3f} n={ol['n']:<4d} "
              f"FULL pf={fl['pf']:.3f} n={fl['n']:<4d} "
              f"base_full={r['base']['full']['pf']:.3f}")

    print(f"\n=== {len(survivors)} survivors "
          f"(IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, OOS n>={MIN_TRADES_OOS}) ===")
    for r in sorted(survivors, key=lambda r: -(r["live"]["oos"]["pf"])):
        print(f"  {r['tag']:22s} IS {json.dumps(r['live']['is'])}")
        print(f"  {'':22s} OOS {json.dumps(r['live']['oos'])}")
        print(f"  {'':22s} base full {json.dumps(r['base']['full'])}")

    out = ROOT / "outputs" / f"v4_{grid_name}_{symbol}_{tf}_20261028e.json"
    out.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "grid": grid_name,
         "n_segments": len(segs),
         "n_bars": int(sum(len(s) for s in segs)),
         "boundary": str(boundary), "n_cells": len(cells),
         "survivors": survivors, "all": rows}, indent=1, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    sys.exit(main())
