"""HOLD/IS GRID over the honest market-entry architectures (2026-10-26).

PROTOCOL (selection-bias control)
----------------------------------
Every pair's deep archive is split at a timestamp:

    HOLD  = bars strictly BEFORE the split   -> the SELECTION set
    IS    = bars from the split onwards       -> the VALIDATION set

A config is only interesting if it clears BOTH halves.  Configs are ranked by
min(hold.pf, is.pf) so a cell cannot win on one half alone.  The 15m MGC
grid that produced candidate #7382 was scored only on the IS window; this
runner re-scores a grid with the selection/validation roles SWAPPED on the
5m archives (which no grid has ever touched) and on the two 15m symbols the
honest screen never covered (MCL, ZF).

BOOKS: base (named fill) / fair (decision close) / live (next bar open,
production MARKET fill).  `live` is the number that matters.

Usage:
  .venv/bin/python research/cycle_20261026/holdout_grid.py \
      "MGC:5m,MNQ:5m,MCL:5m,ZF:5m,6J:5m" MIDPOINT grid.json out.jsonl
  .venv/bin/python research/cycle_20261026/holdout_grid.py \
      "MCL:15m,ZF:15m" 2026-08-20 grid.json out.jsonl
"""
from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import load_bars, metrics, run_books  # noqa: E402

CODE_PATH = ROOT / "research" / "cycle_20261025" / "strategies" / \
    "market_entry_ict_v1.py"
# Optional 5th CLI arg: path to an alternative strategy file (relative to ROOT).
CODE_FILE = Path(sys.argv[5]) if len(sys.argv) > 5 else CODE_PATH
CODE = CODE_FILE.read_text()


def _segment(bars, split_ts, which):
    ts = bars["ts"]
    seg = bars[ts < split_ts] if which == "hold" else bars[ts >= split_ts]
    return seg.reset_index(drop=True)


def _job(args):
    symbol, tf, split_ts, cfg, which = args
    bars = load_bars(symbol, tf, days=0)          # full archive depth
    if bars is None or len(bars) < 400:
        return None
    if split_ts == "MIDPOINT":
        ts = bars["ts"]
        mid = ts.iloc[0] + (ts.iloc[-1] - ts.iloc[0]) / 2
        split_ts = str(mid)[:10]
    seg = _segment(bars, split_ts, which)
    if len(seg) < 400:
        return None
    b, lv, fa, meta = run_books(CODE, seg, cfg, symbol)
    return {"symbol": symbol, "tf": tf, "params": cfg, "seg": which,
            "split_ts": split_ts, "n_bars": int(len(seg)),
            "first": str(seg["ts"].iloc[0]), "last": str(seg["ts"].iloc[-1]),
            "live": metrics(lv), "fair": metrics(fa), "base": metrics(b),
            "meta": meta}


def main() -> int:
    pairs = [p.split(":") for p in sys.argv[1].split(",")]
    split = sys.argv[2]
    grid = json.loads(Path(sys.argv[3]).read_text())
    out_path = Path(sys.argv[4])

    jobs = [(s.upper(), tf, split, cfg, w)
            for s, tf in pairs for cfg in grid for w in ("hold", "is")]
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=12) as ex:
        for r in ex.map(_job, jobs):
            if r:
                rows.append(r)
                with out_path.open("a") as fh:
                    fh.write(json.dumps(r, default=str) + "\n")

    by_key = {}
    for r in rows:
        by_key.setdefault((r["symbol"], r["tf"]), {})[
            (json.dumps(r["params"], sort_keys=True), r["seg"])] = r

    print(f"\n{'sym':4s} {'tf':4s} {'setup':14s} {'params':<62s} "
          f"{'HOLD n':>7} {'pf':>6} {'E[R]':>7} | {'IS n':>6} {'pf':>6} "
          f"{'E[R]':>7}  verdict")
    survivors = []
    for (symbol, tf), d in sorted(by_key.items()):
        cfgs = sorted({k[0] for k in d})
        out = []
        for c in cfgs:
            h, i = d.get((c, "hold")), d.get((c, "is"))
            if not h or not i:
                continue
            hl, il = h["live"], i["live"]
            out.append((min(hl["pf"], il["pf"]), h, i))
        out.sort(key=lambda x: -x[0])
        for mn, h, i in out[:6]:
            p = h["params"]
            ps = " ".join(f"{k}={v}" for k, v in p.items() if k != "setup")
            hl, il = h["live"], i["live"]
            ok = (hl["pf"] >= 1.3 and il["pf"] >= 1.3
                  and hl["n"] >= 20 and il["n"] >= 20)
            print(f"{symbol:4s} {tf:4s} {p.get('setup',''):14s} {ps:<62s} "
                  f"{hl['n']:7d} {hl['pf']:6.3f} {hl['e_r']:+7.3f} | "
                  f"{il['n']:6d} {il['pf']:6.3f} {il['e_r']:+7.3f}  "
                  f"{'*** SURVIVOR' if ok else ''}")
            if ok:
                survivors.append({"symbol": symbol, "tf": tf, "params": p,
                                  "hold": hl, "is": il,
                                  "hold_fair": h["fair"], "is_fair": i["fair"]})
    print(f"\n{len(survivors)} survivors "
          f"(HOLD pf>=1.3 and IS pf>=1.3, n>=20 both)  "
          f"[{time.time() - t0:.0f}s]")
    out_path.with_suffix(".survivors.json").write_text(
        json.dumps(survivors, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
