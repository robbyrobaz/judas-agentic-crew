"""TASK_2867_LIVE_ENTRY_DISTANCE -- type-agnostic live staleness measure.

WHY A SECOND PROBE
-------------------
`task2867_live_stale_entry_probe.py` tried to match the signal entry to a
bar's OHLC. That only works for CLOSE-anchored entries. Most of this book
enters at a LEVEL (PDH/PDL retest, FVG midpoint, OB edge), so "no OHLC match"
was a category error, not evidence of staleness. Corrected here.

THE MEASURE
-----------
For each broker fill, find the decision bar (last bar with ts <= signal ts) and
ask: where does the signal's entry sit relative to that bar's own range?

    dist_ticks = 0                      entry is inside the decision bar
    dist_ticks in (0, ~5]               entry is just outside it -- plausible
    dist_ticks large                    entry references a bar/level from much
                                        further back -- the runtime was handed a
                                        price it could not have known was next

Cross-check the SAME quantity against the realised NT fill: the live order is
a MARKET order, so the distance from the signal entry to the fill tells us
directly how much of the intended geometry the market took away.

READ-ONLY.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import bar_archive  # noqa: E402

LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}
TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 0.0000005}


def load_bars(symbol: str, tf: str) -> pd.DataFrame:
    for cand in (tf, "1h", "15m", "5m"):
        df = bar_archive.read_archive(symbol, cand)
        if df is not None and not df.empty:
            return df
    return pd.DataFrame()


def main() -> None:
    conn = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    df = pd.read_sql_query(
        """
        SELECT t.id tid, t.symbol, t.strategy_id, a.strategy_family, a.params_json,
               s.ts_utc sig_ts, s.entry sig_entry, t.entry_fill,
               t.pnl_dollars, t.exit_reason, t.opened_at
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        LEFT JOIN active_strategies a ON a.id = t.strategy_id
        WHERE t.entry_fill IS NOT NULL AND s.entry IS NOT NULL
        ORDER BY t.opened_at
        """, conn)
    conn.close()
    df = df[df.symbol.isin(LEGAL)].copy()
    df["tf"] = df.params_json.fillna("{}").apply(
        lambda p: str(json.loads(p or "{}").get("timeframe", "1h")).lower().replace(" ", ""))

    cache: dict[tuple[str, str], pd.DataFrame] = {}
    rows = []
    for _, r in df.iterrows():
        key = (r.symbol, r.tf)
        if key not in cache:
            b = load_bars(*key)
            if not b.empty:
                b = b.sort_values("ts").reset_index(drop=True)
                b["ts"] = pd.to_datetime(b["ts"], utc=True)
            cache[key] = b
        b = cache[key]
        if b.empty:
            continue
        ts = pd.to_datetime(r.sig_ts, utc=True)
        idx = int(b["ts"].searchsorted(ts, side="right") - 1)
        if idx < 0:
            continue
        dec = b.iloc[idx]
        tk = TICK[r.symbol]
        entry = float(r.sig_entry)
        lo, hi = float(dec["low"]), float(dec["high"])
        # how many bars back would you have to go for the entry to fall inside
        # that bar's range? -> "entry age in bars", independent of entry type
        age = None
        for back in range(0, min(49, idx + 1)):
            row = b.iloc[idx - back]
            if float(row["low"]) - tk <= entry <= float(row["high"]) + tk:
                age = back
                break
        dist = max(lo - entry, entry - hi, 0.0) / tk
        rows.append({
            "tid": r.tid, "symbol": r.symbol, "tf": r.tf,
            "strategy_id": r.strategy_id, "strategy_family": r.strategy_family,
            "entry_age_bars": age,
            "dist_outside_decision_bar_ticks": round(float(dist), 1),
            "dec_bar_span_ticks": round(float((hi - lo) / tk), 1),
            "fill_vs_signal_ticks": round(float(r.entry_fill - entry) / tk, 1),
            "pnl": r.pnl_dollars, "exit_reason": r.exit_reason, "sig_ts": r.sig_ts,
        })

    out = pd.DataFrame(rows)
    if out.empty:
        print("no archive coverage")
        return

    print("=" * 78)
    print("LIVE SIGNAL ENTRY AGE -- entry falls inside the range of bar N back")
    print("=" * 78)
    print(f"measured: {len(out)}/{len(df)} legal fills")
    n0 = int((out.entry_age_bars == 0).sum())
    print(f"age 0 (entry inside the DECISION bar)  : {n0} ({100.0*n0/len(out):.1f}%)")
    n1 = int((out.entry_age_bars == 1).sum())
    print(f"age 1                                 : {n1} ({100.0*n1/len(out):.1f}%)")
    n2p = int((out.entry_age_bars >= 2).sum())
    nn = int(out.entry_age_bars.isna().sum())
    print(f"age >=2 (references an OLDER bar)      : {n2p} ({100.0*n2p/len(out):.1f}%)")
    print(f"never inside 50 bars (roll/scale/level): {nn} ({100.0*nn/len(out):.1f}%)")
    print(f"median age (resolved): {out.entry_age_bars.median():.0f} bars   "
          f"mean: {out.entry_age_bars.mean():.2f}")
    print()
    print(f"median entry distance OUTSIDE the decision bar: "
          f"{out.dist_outside_decision_bar_ticks.median():.1f} ticks")
    print(f"median decision-bar span: {out.dec_bar_span_ticks.median():.1f} ticks")
    print()
    print("--- by timeframe ---")
    print(f"{'tf':>5} {'n':>5} {'%age0':>7} {'%age>=2':>8} {'med_age':>8} "
          f"{'med_dist_out':>13}")
    for tf, sub in out.groupby("tf"):
        print(f"{tf:>5} {len(sub):>5} {100.0*(sub.entry_age_bars==0).mean():>6.1f}% "
              f"{100.0*(sub.entry_age_bars>=2).mean():>7.1f}% "
              f"{sub.entry_age_bars.median():>8.0f} "
              f"{sub.dist_outside_decision_bar_ticks.median():>13.1f}")
    print()
    print("--- by strategy_family (n>=10) ---")
    for fam, sub in out.groupby("strategy_family"):
        if len(sub) < 10:
            continue
        print(f"  {str(fam)[:42]:<42} n={len(sub):>4} "
              f"%age0={100.0*(sub.entry_age_bars==0).mean():>5.1f} "
              f"med_age={sub.entry_age_bars.median():>3.0f} "
              f"med_dist_out={sub.dist_outside_decision_bar_ticks.median():>7.1f}")
    print()
    print("--- what the market charged vs the strategy's own bar (ticks) ---")
    print(f"{'sym':>5} {'n':>5} {'med|fill-sig|':>14} {'p90':>8} {'max':>8} "
          f"{'mean_adv':>9}")
    for sym, sub in out.groupby("symbol"):
        v = sub.fill_vs_signal_ticks.abs()
        adv = sub[sub.fill_vs_signal_ticks * (1 if sym else 1) > 0]
        print(f"{sym:>5} {len(sub):>5} {v.median():>14.1f} {v.quantile(.9):>8.1f} "
              f"{v.max():>8.1f} {adv.fill_vs_signal_ticks.mean():>9.1f}")
    print()
    out.to_csv("outputs/task2867_live_entry_distance_20261023.csv", index=False)
    print("wrote outputs/task2867_live_entry_distance_20261023.csv")


if __name__ == "__main__":
    main()