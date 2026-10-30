"""Consolidate every honest-fill grid result from cycles 20261029c/29d.

Reports, per (symbol, tf, family, split protocol):
  * the IS-RANKED TOP cell and whether it PASSES the OOS admission test
    (OOS n >= 20 AND OOS live PF > 1.15)  -- OOS is never used to rank
  * every cell that passes, so the shape of the passing region is visible
  * the BT-LURE flag (PF > 3.0 AND WR > 70%)

Usage:
    .venv/bin/python research/cycle_20261029e/analyse_all.py
"""
from __future__ import annotations

import json
import os
from collections import defaultdict

FILES = [
    "research/cycle_20261029c/grid_out1.json",
    "research/cycle_20261029c/grid_out2.json",
    "research/cycle_20261029c/grid_out3.json",
    "research/cycle_20261029d/out.json",
]


def tag(c: dict) -> str:
    keys = ("pool_mode", "trend_mode", "mit50", "lookback", "min_sweep_atr",
            "disp_k", "rr", "confirm_bars", "body_ratio_min", "stop_buf_atr")
    return " ".join(f"{k}={c.get(k)}" for k in keys if k in c)


def main() -> int:
    groups: dict = defaultdict(list)
    for f in FILES:
        if not os.path.exists(f):
            print(f"MISSING {f}")
            continue
        for r in json.load(open(f)):
            groups[(r.get("symbol"), r.get("tf"), r.get("label"),
                    r.get("split_mode"))].append(r)

    summary = []
    for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        sym, tf, label, split = key
        rows = [r for r in groups[key] if r.get("live_IS")]
        if not rows:
            continue
        ranked = sorted(rows, key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                            -(r["live_IS"].get("n") or 0)))
        passers = [r for r in rows
                   if (r.get("live_OOS") or {}).get("n", 0) >= 20
                   and (r.get("live_OOS") or {}).get("pf", 0) > 1.15]
        passers.sort(key=lambda r: -(r["live_IS"].get("pf") or 0))
        top = ranked[0]
        li, lo, lv = top["live_IS"], top["live_OOS"], top["live"]
        ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
        lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
        print(f"\n===== {label} {sym}_{tf} split={split} ({len(rows)} cells)")
        print(f"  IS-TOP: {'PASS' if ok else 'FAIL'}  IS n={li.get('n')} "
              f"PF={li.get('pf'):.3f} ${li.get('total_usd', 0):.0f} | "
              f"OOS n={lo.get('n')} PF={lo.get('pf'):.3f} ${lo.get('total_usd', 0):.0f} "
              f"| ALL n={lv.get('n')} PF={lv.get('pf'):.3f} WR={lv.get('wr_pct')}% "
              f"DD=${lv.get('max_dd', 0):.0f}{'  BT-LURE' if lure else ''}")
        print(f"        {tag(top.get('cell') or {})}")
        if passers:
            print(f"  -- PASSERS ({len(passers)}) --")
            for r in passers[:8]:
                li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
                lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
                print(f"    IS n={li.get('n')} PF={li.get('pf'):.3f} "
                      f"${li.get('total_usd', 0):.0f} | OOS n={lo.get('n')} "
                      f"PF={lo.get('pf'):.3f} ${lo.get('total_usd', 0):.0f} "
                      f"WR={lo.get('wr_pct')}% | ALL PF={lv.get('pf'):.3f} "
                      f"WR={lv.get('wr_pct')}% DD=${lv.get('max_dd', 0):.0f}"
                      f"{'  BT-LURE' if lure else ''}")
                print(f"       {tag(r.get('cell') or {})}")
                summary.append({
                    "symbol": sym, "tf": tf, "label": label, "split": split,
                    "cell": r.get("cell"), "live": lv, "live_IS": li,
                    "live_OOS": lo, "lure": lure,
                })
    out = "research/cycle_20261029e/passers.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(summary, fh, indent=1)
    print(f"\nwrote {out} ({len(summary)} passing cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
