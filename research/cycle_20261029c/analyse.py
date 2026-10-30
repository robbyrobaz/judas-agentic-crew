"""Analyse the honest grid output: list cells that PASS the OOS admission test.

PASS = OOS n >= 20 AND OOS live PF > 1.15 (OOS is never used to rank).
Also reports the IS-selected cell per symbol/tf and whether IT passes, so we
never cherry-pick a cell because of its OOS number.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

KEYS = ("pool_mode", "mit50", "lookback", "cluster_age", "min_sweep_atr",
        "disp_k", "rr", "confirm_bars", "body_ratio_min")


def tag(c):
    return " ".join(f"{k}={c.get(k)}" for k in KEYS if k in c)


def main() -> int:
    res = json.loads(Path(sys.argv[1]).read_text())
    groups = {}
    for r in res:
        groups.setdefault((r["symbol"], r["tf"], r.get("label", "")), []).append(r)

    for (sym, tf, label), rows in groups.items():
        rows = [r for r in rows if r.get("live_IS")]
        if not rows:
            continue
        ranked = sorted(rows, key=lambda r: (-(r["live_IS"].get("pf") or 0),
                                            -(r["live_IS"].get("n") or 0)))
        print(f"\n================ {label} {sym}_{tf} "
              f"({len(rows)} cells)")
        print("-- PASSERS (OOS n>=20 and OOS PF>1.15) --")
        passers = []
        for r in rows:
            lo = r["live_OOS"]
            if (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15:
                passers.append(r)
        passers.sort(key=lambda r: -(r["live_IS"].get("pf") or 0))
        for r in passers:
            li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
            lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
            print(f"  IS n={li.get('n')} PF={li.get('pf'):.3f} ${li.get('total_usd',0):.0f}"
                  f" | OOS n={lo.get('n')} PF={lo.get('pf'):.3f} ${lo.get('total_usd',0):.0f}"
                  f" WR={lo.get('wr_pct')}% | ALL n={lv.get('n')} PF={lv.get('pf'):.3f}"
                  f" WR={lv.get('wr_pct')}% DD=${lv.get('max_dd',0):.0f}"
                  f"{'  BT-LURE!' if lure else ''}")
            print(f"      {tag(r['cell'])}")
        print("-- IS-RANKED TOP 5 (the cell a blind protocol would pick) --")
        for r in ranked[:5]:
            li, lo, lv = r["live_IS"], r["live_OOS"], r["live"]
            ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
            print(f"  {'PASS' if ok else 'fail'}  IS n={li.get('n')} PF={li.get('pf'):.3f}"
                  f" | OOS n={lo.get('n')} PF={lo.get('pf'):.3f}"
                  f" | ALL PF={lv.get('pf'):.3f}  {tag(r['cell'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
