"""CROSS-SYMBOL 5m sweep->displacement port test (2026-10-28).

WHY
---
The MGC 5m sweep->displacement cell (lb=20 ms=0.6 dk=1.0 rr=2.0, plus the
tighter-sweep variant lb=20 ms=0.75 dk=1.0 rr=2.0) survived an IS/OOS time
split on the LIVE/production fill.  The 15m cross-symbol ports of the same
architecture were killed (MNQ 0.758 / MCL 0.685 / ZF 0.812 / 6J 1.017) because
those archives are polluted with PLACEHOLDER rows -- the hygiene guard inside
the signal code fixes that.  So the question this script answers is: does the
edge PORT once the pollution is removed, or is it MGC-only?

Same protocol as the MGC grid:
  * `live` book = PRODUCTION fill (market at the OPEN of the bar AFTER the
    decision bar, brackets re-anchored, gap-aware exits),
    cost_model=v1_realistic_micros, qty=1.
  * per CLEAN SEGMENT (placeholder rows dropped) so a trade can never jump a
    time gap; trades from all segments are aggregated.
  * TIME split over the global clean span: first 60% = IS, last 40% = OOS.
    The OOS book is the admission test (n_oos >= 20 and OOS live PF > 1.15).

Usage:
  .venv/bin/python research/cycle_20261028/xport_5m.py
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments  # noqa: E402

HYGIENE = (ROOT / "research" / "cycle_20261028" / "strategies" /
           "sweep_cont_hygiene_5m_v1.py").read_text()

IS_FRAC = 0.60
MIN_SEG = 300

CELLS = [
    {"lookback": 20, "min_sweep_atr": 0.60, "disp_k": 1.0, "rr": 2.0,
     "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
     "atr_period": 14},
    {"lookback": 20, "min_sweep_atr": 0.75, "disp_k": 1.0, "rr": 2.0,
     "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
     "atr_period": 14},
    {"lookback": 30, "min_sweep_atr": 0.60, "disp_k": 1.0, "rr": 2.0,
     "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
     "atr_period": 14},
]

SYMBOLS = ["MNQ", "MCL", "ZF", "6J"]


def _one(job):
    symbol, tf, cell = job
    segs = clean_segments(symbol, tf, MIN_SEG)
    if not segs:
        return symbol, tf, cell, {"error": "no clean segment >= %d bars" % MIN_SEG}
    live, base = [], []
    t0 = min(s["ts"].iloc[0] for s in segs)
    t1 = max(s["ts"].iloc[-1] for s in segs)
    split = t0 + (t1 - t0) * IS_FRAC
    timed_out = False
    for seg in segs:
        b_t, l_t, _f_t, meta = run_books(HYGIENE, seg, cell, symbol, budget_s=900.0)
        if meta.get("load_error"):
            return symbol, tf, cell, {"error": meta.get("error")}
        if meta.get("timed_out"):
            timed_out = True
        seg_ts = seg["ts"].reset_index(drop=True)
        for book in (b_t, l_t):
            for t in book:
                try:
                    t["entry_ts"] = seg_ts.iloc[int(t["entry_bar"])]
                except Exception:  # noqa: BLE001
                    t["entry_ts"] = t0
        base += b_t
        live += l_t
    out = {"split": str(split), "timed_out": timed_out,
           "n_segments": len(segs),
           "first_bar": str(segs[0]["ts"].iloc[0]),
           "last_bar": str(segs[-1]["ts"].iloc[-1]),
           "n_bars": int(sum(len(s) for s in segs))}
    for name, book in (("live", live), ("base", base)):
        is_t = [t for t in book if t["entry_ts"] < split] if book else []
        oos_t = [t for t in book if t["entry_ts"] >= split] if book else []
        out[name] = {"is": metrics(is_t), "oos": metrics(oos_t), "all": metrics(book)}
    return symbol, tf, cell, out


def main():
    jobs = [(s, "5m", c) for s in SYMBOLS for c in CELLS]
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for symbol, tf, cell, res in ex.map(_one, jobs):
            if "error" in res:
                print(symbol, cell, res, flush=True)
                continue
            li, lo, la = res["live"]["is"], res["live"]["oos"], res["live"]["all"]
            rows.append({"symbol": symbol, "tf": tf, "cell": cell, **res})
            print(f"{symbol} lb={cell['lookback']:>2} ms={cell['min_sweep_atr']:.2f} "
                  f"dk={cell['disp_k']:.1f} rr={cell['rr']:.1f} | "
                  f"segs={res['n_segments']} bars={res['n_bars']} "
                  f"{res['first_bar'][:10]}->{res['last_bar'][:10]} | "
                  f"IS n={li['n']:>3} pf={li['pf']:>6} | "
                  f"OOS n={lo['n']:>3} pf={lo['pf']:>6} e_r={lo['e_r']:>6} | "
                  f"ALL n={la['n']:>3} pf={la['pf']:>6} usd={la['total_usd']:>9,.0f}",
                  flush=True)
    out = ROOT / "outputs" / "xport_5m_20261028.json"
    out.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out)


if __name__ == "__main__":
    main()
