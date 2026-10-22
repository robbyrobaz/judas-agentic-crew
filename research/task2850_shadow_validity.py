"""Task #2850 part 3 -- VALIDITY ATTACKS on the shadow ledger.

The first pass printed book-wide shadow PF 2.876 / avg R +0.515, which is far
too good to accept at face value. Three candidate artefacts:

  A1 FICTIONAL FILLS. `entry` is the price the detector published. If it is a
     limit/stop level the market never trades back through, the trade never
     existed and its outcome is fiction. Measure: does any bar between the
     signal and the exit actually contain `entry`?
  A2 DUPLICATE ROWS. ~176 actives include many rows that are the SAME
     architecture (symbol|csid|tf) firing on the same bar, so book-level
     aggregation double/triple counts one economic bet.
  A3 REGIME / CLUSTERING. A 9-day window in one trending regime; are the wins
     clustered into a few days or hours?

READ-ONLY.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "cache_1h"
LEDGER = REPO / "research" / "task2850_shadow_ledger.json"


def load_bars(symbol: str, tf: str):
    p = CACHE / f"{symbol}_{tf}.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def main() -> None:
    t = pd.read_json(LEDGER)
    t = t[t.outcome.isin(["WIN", "LOSS", "OPEN"])].copy()
    t["ts_utc"] = pd.to_datetime(t["ts_utc"], utc=True, format="ISO8601")

    # ---------------------------------------------------------- A1 fill test
    cache: dict[tuple[str, str], dict] = {}

    def arr(sym, tf):
        key = (sym, tf)
        if key not in cache:
            df = load_bars(sym, tf)
            cache[key] = {"ts": np.array([x.value for x in df["ts"]], dtype="int64"),
                          "hi": df.high.to_numpy(float), "lo": df.low.to_numpy(float),
                          "cl": df.close.to_numpy(float), "op": df.open.to_numpy(float)}
        return cache[key]

    filled = np.zeros(len(t), dtype=bool)
    gap_at_signal = np.zeros(len(t))
    dist_ticks = np.zeros(len(t))
    TICK = {"MGC": 0.1, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 5e-07}
    for n, r in enumerate(t.itertuples()):
        a = arr(r.symbol, r.tf)
        i = int(np.searchsorted(a["ts"], pd.Timestamp(r.ts_utc).tz_convert("UTC").value, side="right"))
        k = int(r.k) if r.k is not None and r.k >= 0 else len(a["ts"]) - 1
        if i > k:
            continue
        seg_lo, seg_hi = a["lo"][i:k + 1], a["hi"][i:k + 1]
        filled[n] = bool(np.any((seg_lo <= r.entry) & (seg_hi >= r.entry)))
        gap_at_signal[n] = abs(float(a["cl"][i - 1]) - r.entry) if i > 0 else np.nan
        dist_ticks[n] = abs(float(a["cl"][i - 1]) - r.entry) / TICK[r.symbol] if i > 0 else np.nan
    t["filled"] = filled
    t["entry_dist_ticks"] = dist_ticks
    t["entry_dist_ticks"].replace(np.inf, np.nan, inplace=True)

    print("=" * 100)
    print("A1  FICTIONAL-FILL TEST  (does the market actually trade through `entry`?)")
    print("=" * 100)
    print(f"resolved trades          : {len(t)}")
    print(f"entry never touched      : {int((~t.filled).sum())}  ({100*(~t.filled).mean():.1f}%)")
    print("\nby outcome (PF computed the WRONG way = including unfilled, and the "
          "FILTERED way = filled only):")
    for lab, x in t.groupby("outcome"):
        w, l = x.pnl.clip(lower=0).sum(), -x.pnl.clip(upper=0).sum()
        print(f"  {lab:<5} n={len(x):<6} pf_all={w/l if l else 0:>7.3f}")
    for lab, x in t.groupby("filled"):
        w, l = x.pnl.clip(lower=0).sum(), -x.pnl.clip(upper=0).sum()
        wr = (x.outcome == "WIN").mean()
        print(f"  filled={str(lab):<5} n={len(x):<6} pf={w/l if l else 0:>7.3f} "
              f"WR={wr*100:>5.1f}%  ${x.pnl.sum():>11,.0f}  avgR={x.r_mult.mean():+.3f}")
    print("\nfilled-only by symbol:")
    for s, x in t[t.filled].groupby("symbol"):
        w, l = x.pnl.clip(lower=0).sum(), -x.pnl.clip(upper=0).sum()
        print(f"  {s:<5} n={len(x):<6} pf={w/l if l else 0:>7.3f} "
              f"WR={(x.outcome=='WIN').mean()*100:>5.1f}%  ${x.pnl.sum():>11,.0f}  "
              f"avgR={x.r_mult.mean():+.3f}")
    print("\nfilled-only by session:")
    for s, x in t[t.filled].groupby("session"):
        w, l = x.pnl.clip(lower=0).sum(), -x.pnl.clip(upper=0).sum()
        print(f"  {s:<8} n={len(x):<6} pf={w/l if l else 0:>7.3f} "
              f"WR={(x.outcome=='WIN').mean()*100:>5.1f}%  ${x.pnl.sum():>11,.0f}")
    print("\nentry distance from last close at signal time (ticks), filled-only:")
    print(t[t.filled].groupby("symbol").entry_dist_ticks.describe()[
        ["count", "mean", "50%", "max"]].round(2).to_string())
    # unrealistic-entry check: entries far from market
    far = t[t.filled & (t.entry_dist_ticks > 40)]
    print(f"\ntrades whose entry sits >40 ticks from the signal-bar close: {len(far)} "
          f"({100*len(far)/max(len(t),1):.1f}%)  pf="
          f"{(far.pnl.clip(lower=0).sum()/-far.pnl.clip(upper=0).sum()) if len(far) else 0:.3f} "
          f"${far.pnl.sum():,.0f}")
    near = t[t.filled & (t.entry_dist_ticks <= 5)]
    print(f"trades whose entry sits <=5 ticks from the signal-bar close : {len(near)}  "
          f"pf={(near.pnl.clip(lower=0).sum()/-near.pnl.clip(upper=0).sum()) if len(near) else 0:.3f} "
          f"${near.pnl.sum():,.0f}")

    # ------------------------------------------------- A2 duplicate inflation
    print("\n" + "=" * 100)
    print("A2  DUPLICATE-ROW INFLATION  (same architecture = symbol|csid|tf)")
    print("=" * 100)
    t["arch"] = t.symbol + "|" + t.csid.astype(str) + "|" + t.tf
    print(f"distinct strategy rows signalled : {t.strategy_id.nunique()}")
    print(f"distinct architectures           : {t.arch.nunique()}")
    per_arch = t.groupby("arch").strategy_id.nunique()
    print(f"architectures fired by >1 row     : {int((per_arch>1).sum())} "
          f"(max rows/arch = {int(per_arch.max())})")
    print(f"median rows per architecture      : {per_arch.median():.1f}")
    # dedup: keep ONE representative trade per (arch, timestamp) -- the same
    # economic bet can only be taken once.
    d = t.sort_values("strategy_id").drop_duplicates(subset=["arch", "ts_utc"], keep="first")
    w, l = d.pnl.clip(lower=0).sum(), -d.pnl.clip(upper=0).sum()
    print(f"\nnaive (all rows)          : n={len(t):<6} pf={w/l if l else 0:.3f}  ... ", end="")
    w2, l2 = t.pnl.clip(lower=0).sum(), -t.pnl.clip(upper=0).sum()
    print(f"(pf={w2/l2 if l2 else 0:.3f}, ${t.pnl.sum():,.0f})")
    print(f"deduped (1 per arch+ts)   : n={len(d):<6} pf={w/l if l else 0:.3f}  "
          f"WR={(d.outcome=='WIN').mean()*100:.1f}%  ${d.pnl.sum():,.0f}  avgR={d.r_mult.mean():+.3f}")
    df2 = d[d.filled]
    w3, l3 = df2.pnl.clip(lower=0).sum(), -df2.pnl.clip(upper=0).sum()
    print(f"deduped + filled-only     : n={len(df2):<6} pf={w3/l3 if l3 else 0:.3f}  "
          f"WR={(df2.outcome=='WIN').mean()*100:.1f}%  ${df2.pnl.sum():,.0f}  "
          f"avgR={df2.r_mult.mean():+.3f}")

    # ---------------------------------------------------- A3 time clustering
    print("\n" + "=" * 100)
    print("A3  IS THE EDGE CONCENTRATED IN A FEW DAYS?  (deduped, filled-only)")
    print("=" * 100)
    df2 = df2.copy()
    df2["day"] = df2.ts_utc.dt.date
    for day, x in df2.groupby("day"):
        w, l = x.pnl.clip(lower=0).sum(), -x.pnl.clip(upper=0).sum()
        print(f"  {day}  n={len(x):<5} pf={w/l if l else 0:>6.3f}  ${x.pnl.sum():>9,.0f}  "
              f"avgR={x.r_mult.mean():+.3f}")

    t.to_csv(REPO / "research" / "task2850_shadow_trades_enriched.csv", index=False)
    print(f"\nwrote {REPO/'research'/'task2850_shadow_trades_enriched.csv'}")


if __name__ == "__main__":
    main()