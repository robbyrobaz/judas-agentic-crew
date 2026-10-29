"""HONEST LIVE-BOOK VERIFIER for a strategy CODE FILE (2026-10-29a cycle).

Runs a candidate strategy file through the production-fill harness:
  * CLEAN SEGMENTS only (placeholder / dirty bars dropped),
  * three books from the SAME signals:
      base = fill at the named price (what the house backtester stamps),
      fair = fill at the decision bar's close,
      live = MARKET at the next bar's open, brackets re-anchored, gap-aware
             exits  <-- the PRODUCTION fill, the number we rank on,
  * IS/OOS split BY TIME over the global clean span (first 60% IS),
  * a geometry guard on every signal.

This is the verifier used to certify the wick-rejection port and to run the
CROSS-SYMBOL TRANSFER of the validated cell.

usage: .venv/bin/python research/cycle_20261029a/verify.py <code.py> <SYM> <tf> [tag]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261026"))

from honest_screen import metrics, run_books  # noqa: E402
from seg_sweep import clean_segments, split_ts  # noqa: E402

MIN_TRADES_IS = 20
MIN_TRADES_OOS = 15


def main() -> int:
    code_path = Path(sys.argv[1])
    symbol = sys.argv[2].upper()
    tf = sys.argv[3]
    tag = sys.argv[4] if len(sys.argv) > 4 else code_path.stem
    CODE = code_path.read_text()

    segs = clean_segments(symbol, tf)
    if not segs:
        print(f"{symbol} {tf}: NO clean segment >= 300 bars")
        return 1
    boundary = split_ts(symbol, tf, segs)
    print(f"{symbol} {tf}: {len(segs)} clean segments, "
          f"{sum(len(s) for s in segs)} bars, boundary {boundary}", flush=True)

    per = {"base": [], "live": [], "fair": []}
    t0 = time.time()
    for seg in segs:
        b, lv, fa, meta = run_books(CODE, seg, {}, symbol)
        if meta.get("load_error"):
            print("LOAD ERROR", meta)
            return 2
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

    out = {"symbol": symbol, "tf": tf, "tag": tag, "code": str(code_path),
           "n_segments": len(segs),
           "n_bars": int(sum(len(s) for s in segs)),
           "segments": [[str(s["ts"].iloc[0]), str(s["ts"].iloc[-1]), int(len(s))]
                        for s in segs],
           "boundary": str(boundary), "elapsed_s": round(time.time() - t0, 1)}
    for kind in ("live", "base", "fair"):
        is_t, oos_t = partition(kind)
        out[kind] = {"is": metrics(is_t), "oos": metrics(oos_t),
                     "full": metrics(is_t + oos_t)}

    il, ol, fl = out["live"]["is"], out["live"]["oos"], out["live"]["full"]
    print(f"\n=== {symbol} {tf} :: {tag} (LIVE book) ===")
    for nm, m in (("IS ", il), ("OOS", ol), ("ALL", fl)):
        print(f"  {nm} n={m['n']:<4d} pf={m['pf']:<6.3f} wr={m['wr_pct']:<5.1f}% "
              f"e_r={m['e_r']:<6.3f} usd={m['total_usd']:<10.2f} "
              f"dd={m['max_dd']:<9.2f} held={m['avg_bars_held']:.1f} "
              f"stopgap={m['n_stop_gap']}")
    print(f"  base full pf={out['base']['full']['pf']:.3f} n={out['base']['full']['n']}  "
          f"fair full pf={out['fair']['full']['pf']:.3f} n={out['fair']['full']['n']}")
    ok = (il["pf"] > 1.0 and ol["pf"] > 1.0 and il["n"] >= MIN_TRADES_IS
          and ol["n"] >= MIN_TRADES_OOS)
    print(f"  SURVIVOR: {ok}  (IS pf>1 & OOS pf>1, IS n>={MIN_TRADES_IS}, "
          f"OOS n>={MIN_TRADES_OOS})")

    p = ROOT / "outputs" / f"verify_{tag}_{symbol}_{tf}_20261029a.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
