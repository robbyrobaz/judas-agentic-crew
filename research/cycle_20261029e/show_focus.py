"""Print the single-cell honest-fill envelopes from cycles 20261029c outA/outB."""
from __future__ import annotations

import json
import os


def show(path):
    if not os.path.exists(path):
        print(f"MISSING {path}")
        return
    for r in json.load(open(path)):
        li = r.get("live_IS") or {}
        lo = r.get("live_OOS") or {}
        lv = r.get("live") or {}
        ok = (lo.get("n") or 0) >= 20 and (lo.get("pf") or 0) > 1.15
        lure = (lv.get("pf") or 0) > 3.0 and (lv.get("wr_pct") or 0) > 70
        print(f"{r.get('label'):26s} {r.get('symbol')}_{r.get('tf'):4s} "
              f"split={str(r.get('split_mode')):12s} "
              f"IS n={li.get('n')} PF={li.get('pf')} ${li.get('total_usd', 0):.0f} | "
              f"OOS n={lo.get('n')} PF={lo.get('pf')} ${lo.get('total_usd', 0):.0f} | "
              f"ALL n={lv.get('n')} PF={lv.get('pf')} WR={lv.get('wr_pct')}% "
              f"${lv.get('total_usd', 0):.0f} DD=${lv.get('max_dd', 0):.0f} "
              f"{'PASS' if ok else 'fail'}{' BT-LURE' if lure else ''}")
        print(f"    params={r.get('params')}")


if __name__ == "__main__":
    show("research/cycle_20261029c/outA.json")
    print("-" * 100)
    show("research/cycle_20261029c/outB.json")
