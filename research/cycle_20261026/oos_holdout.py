"""TRUE OUT-OF-SAMPLE HOLDOUT for the market-entry architecture (2026-10-26).

WHY
---
Finding #9145 established the honest (market-entry) numbers for the
sweep -> displacement continuation family: PF ~1.2-1.35 on the 120d window
that the archive held at the time.  It also said the IS/OOS split was "the
next gate before any promotion", because a max-over-362-cells number scored
on ONE window is selection-biased.

The MGC 15m deep archive has since been extended BACKWARDS (see
extend_archive.py) so that the older half is a genuine holdout for any
parameter chosen on the recent half.  This script runs the 2026-10-25/26
honest_screen books (base / fair / live) over BOTH halves and reports the
survivors.

SPLIT
-----
  HOLD = bars strictly BEFORE `split_ts`   (never seen by the grid search)
  IS   = bars from `split_ts` onwards       (the window the grid was scored on)

A config is only interesting if it clears PF>1 in BOTH halves with a decent
sample.  The LIVE book (entry = next bar OPEN, brackets re-anchored) is the
production fill -- finding #9134: there is no LIMIT entry path in this repo.

Usage:
  .venv/bin/python research/cycle_20261026/oos_holdout.py \
      MGC 15m 2026-06-11 'json-list-of-configs'
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from src import bar_archive  # noqa: E402
from honest_screen import load_bars, metrics, run_books  # noqa: E402

CODE_PATH = ROOT / "research" / "cycle_20261025" / "strategies" / \
    "market_entry_ict_v1.py"
# Optional 5th CLI arg: path to an alternative strategy file (relative to ROOT).
CODE_FILE = Path(sys.argv[5]) if len(sys.argv) > 5 else CODE_PATH
CODE = CODE_FILE.read_text()


def _segment(bars, split_ts, which):
    ts = bars["ts"]
    if which == "hold":
        seg = bars[ts < split_ts]
    else:
        seg = bars[ts >= split_ts]
    return seg.reset_index(drop=True)


def _job(args):
    symbol, tf, split_ts, cfg, which = args
    bars = load_bars(symbol, tf, days=0)          # full archive depth
    if bars is None or len(bars) < 400:
        return None
    seg = _segment(bars, split_ts, which)
    if len(seg) < 400:
        return None
    b, lv, fa, meta = run_books(CODE, seg, cfg, symbol)
    return {"symbol": symbol, "tf": tf, "params": cfg, "seg": which,
            "first": str(seg["ts"].iloc[0]), "last": str(seg["ts"].iloc[-1]),
            "n_bars": int(len(seg)),
            "live": metrics(lv), "fair": metrics(fa), "base": metrics(b),
            "meta": meta}


def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    split_ts = sys.argv[3]
    configs = json.loads(sys.argv[4]) if len(sys.argv) > 4 else []
    if not configs:
        print("no configs")
        return 1

    jobs = [(symbol, tf, split_ts, c, w)
            for c in configs for w in ("hold", "is")]
    rows = []
    with ProcessPoolExecutor(max_workers=12) as ex:
        for r in ex.map(_job, jobs):
            if r:
                rows.append(r)

    if not rows:
        print("no rows (archive too short?)")
        return 1
    hb = [r for r in rows if r["seg"] == "hold"][0]
    print(f"=== {symbol} {tf}  HOLD {hb['first']} -> {hb['last']} "
          f"({hb['n_bars']} bars) | IS {split_ts} -> end ===")
    print(f"  {len(configs)} configs; grid was scored on the IS window only")

    out = []
    for cfg in configs:
        rh = next((r for r in rows if r["params"] == cfg and r["seg"] == "hold"), None)
        ri = next((r for r in rows if r["params"] == cfg and r["seg"] == "is"), None)
        if not rh or not ri:
            continue
        hl, il = rh["live"], ri["live"]
        hf, iff = rh["fair"], ri["fair"]
        out.append({"params": cfg, "hold": hl, "is": il,
                    "hold_fair": hf, "is_fair": iff,
                    "meta": {"hold": rh["meta"], "is": ri["meta"]}})

    def key(r):
        return -(min(r["hold"]["pf"], r["is"]["pf"]))
    out.sort(key=key)

    print(f"\n{'params':<78} {'HOLD n':>7} {'pf':>6} {'E[R]':>6} "
          f"{'IS n':>6} {'pf':>6} {'E[R]':>6}  verdict")
    survivors = []
    for r in out:
        hl, il = r["hold"], r["is"]
        tag = json.dumps({k: v for k, v in r["params"].items()
                          if k not in ("atr_period", "confirm_bars")})
        ok = (hl["pf"] > 1.0 and il["pf"] > 1.0
              and hl["n"] >= 20 and il["n"] >= 20)
        if ok:
            survivors.append(r)
        print(f"{tag:<78} {hl['n']:>7} {hl['pf']:>6.3f} {hl['e_r']:>+6.3f} "
              f"{il['n']:>6} {il['pf']:>6.3f} {il['e_r']:>+6.3f}  "
              f"{'SURVIVOR' if ok else ''}")

    print(f"\n=== {len(survivors)} survivors (HOLD pf>1 & IS pf>1, n>=20 both) ===")
    for r in survivors:
        print(f"  {json.dumps(r['params'])}")
        print(f"      HOLD live {json.dumps(r['hold'])}")
        print(f"      IS   live {json.dumps(r['is'])}")

    p = ROOT / "outputs" / f"oos_holdout_{symbol}_{tf}_{split_ts}.json"
    p.write_text(json.dumps(
        {"symbol": symbol, "tf": tf, "split_ts": split_ts,
         "hold_first": hb["first"], "hold_last": hb["last"],
         "n_configs": len(configs), "rows": out,
         "n_survivors": len(survivors)}, indent=2, default=str))
    print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
