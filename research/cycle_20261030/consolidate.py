"""CONSOLIDATE every honest-fill grid result (task #2893, 2026-10-30 cycle).

PROTOCOL (unchanged from 20261029c/d/e):
  * FILL   = `live` book: MARKET at the OPEN of the bar AFTER the decision bar,
             brackets re-anchored, gap-aware exits (PRODUCTION semantics).
  * COSTS  = v1_realistic_micros, dollar-denominated, qty as intended.
  * DATA   = per clean segment (placeholder rows dropped).
  * SPLIT  = global_time and/or per_segment; both are reported.
  * SELECT = rank cells on IS ONLY (live_IS pf, then live_IS n).
  * ADMIT  = the IS-TOP cell must ALSO pass the OOS test:
             OOS n >= 20 AND OOS live PF > 1.15.
  * LURE   = ALL live PF > 3.0 AND ALL live WR > 70%  -> BT-LURE, not a selling point.

This script prints one line per (code, symbol, tf, split) group with the
IS-selected cell, its OOS verdict, and the dollar throughput per day/week so
the candidate ranking is by DOLLARS, not by PF.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

FILES = [
    "research/cycle_20261029c/grid_out1.json",
    "research/cycle_20261029c/grid_out2.json",
    "research/cycle_20261029c/grid_out3.json",
    "research/cycle_20261029d/out.json",
    "research/cycle_20261029e/out_a.json",
    "research/cycle_20261029e/out_b.json",
    "research/cycle_20261029e/out_c.json",
]

TAG_KEYS = ("setup", "pool_mode", "trend_mode", "mit50", "disp_mode",
            "lookback", "min_sweep_atr", "disp_k", "rr", "confirm_bars",
            "body_ratio_min", "stop_buf_atr", "atr_period", "cluster_age",
            "hist_bars", "pivot_len", "cluster_atr", "min_pivots",
            "sweep_lookback", "fvg_expiry", "target_mode", "liq_lb",
            "pierce_atr", "mss_win", "mss_lb", "fvg_min_atr",
            "require_fvg_tap", "max_r", "min_r", "buf_atr")


def tag(cell: dict) -> str:
    return " ".join(f"{k}={cell[k]}" for k in TAG_KEYS if k in cell)


def _days(a: str, b: str) -> float:
    try:
        d1 = datetime.fromisoformat(str(a).replace("+00:00", "+00:00"))
        d2 = datetime.fromisoformat(str(b).replace("+00:00", "+00:00"))
        if d1.tzinfo is None:
            d1 = d1.replace(tzinfo=timezone.utc)
        if d2.tzinfo is None:
            d2 = d2.replace(tzinfo=timezone.utc)
        return max((d2 - d1).total_seconds() / 86400.0, 1e-9)
    except Exception:  # noqa: BLE001
        return float("nan")


def main() -> int:
    groups: dict = defaultdict(list)
    for f in FILES:
        if not os.path.exists(f):
            print(f"MISSING {f}")
            continue
        for r in json.load(open(f)):
            if not r.get("live_IS"):
                continue
            split = r.get("split_mode") or "global_time"
            key = (r.get("code_file"), r.get("symbol"), r.get("tf"), split)
            groups[key].append(r)

    admitted = []
    for key in sorted(groups, key=lambda k: (str(k[1]), str(k[2]), str(k[3]), str(k[0]))):
        code, sym, tf, split = key
        rows = groups[key]
        ranked = sorted(rows, key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                            -(r["live_IS"].get("n") or 0)))
        top = ranked[0]
        li, lo, lv = top["live_IS"], top["live_OOS"], top["live"]
        ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
        lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
        days = _days(top.get("seg_first"), top.get("seg_last"))
        per_day = lv.get("total_usd", 0) / days if days == days else float("nan")
        trades_wk = (lv.get("n") or 0) / days * 7 if days == days else float("nan")
        flag = "ADMIT" if ok else "fail "
        print(f"{flag} {sym:4s} {tf:3s} {split:12s} cells={len(rows):3d} "
              f"IS n={li.get('n'):>4} PF={li.get('pf'):>6.3f} | "
              f"OOS n={lo.get('n'):>4} PF={lo.get('pf'):>6.3f} | "
              f"ALL n={lv.get('n'):>4} PF={lv.get('pf'):>6.3f} "
              f"WR={lv.get('wr_pct'):>5} ${lv.get('total_usd'):>8.0f} "
              f"DD=${lv.get('max_dd'):>7.0f} | ${per_day:6.1f}/d "
              f"{trades_wk:4.1f} trades/wk | {os.path.basename(str(code))}")
        print(f"       {tag(top.get('cell') or {})}")
        if ok:
            admitted.append({
                "code_file": code, "symbol": sym, "tf": tf, "split": split,
                "cell": top.get("cell"), "live": lv, "live_IS": li,
                "live_OOS": lo, "lure": lure, "n_cells": len(rows),
                "span_days": round(days, 1),
                "usd_per_day": round(per_day, 2) if per_day == per_day else None,
                "trades_per_week": round(trades_wk, 2) if trades_wk == trades_wk else None,
                "seg_first": top.get("seg_first"), "seg_last": top.get("seg_last"),
                "n_segments": top.get("n_segments"),
                "segment_bars": top.get("segment_bars"),
            })

    admitted.sort(key=lambda a: -(a["usd_per_day"] or 0))
    out = "research/cycle_20261030/admitted.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(admitted, fh, indent=1)
    print(f"\nwrote {out} ({len(admitted)} admitted IS-selected cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
