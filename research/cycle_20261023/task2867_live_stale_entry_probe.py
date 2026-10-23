"""TASK_2867_LIVE_STALE_ENTRY_PROBE -- does the LIVE signal entry come from a
bar that had ALREADY CLOSED when the order went out?

CONTEXT
-------
TASK_2867_LEG_ANCHOR_AUDIT measured that the recorded stop/target sit a median
30-47 ticks away (MGC/MNQ) from the levels the broker held, and that the
signal->order latency is exactly 0.0 minutes on all 688 legal-symbol fills.
Zero latency rules out "we waited too long". The remaining explanation is the
one finding 07f3a4c2 named for the BACKTEST: the strategy's returned `entry`
is taken from an OLD bar inside `evaluate(bars)`.

THE TEST (uses only real archived bars, no replay, no strategy code)
--------------------------------------------------------------------
For each broker fill, take the signal ts, load the strategy's own timeframe
from the deep archive, and find the LAST BAR WHOSE ts <= signal_ts (the
decision bar). Then:

    age_bars = how many whole bars before the decision bar the signal's
               `entry` matches (close / high / low / open of that bar)

If the entry matches the decision bar's own close, the strategy is
decision-time correct and the live path is clean. If it matches a bar N>0
back, the strategy is handing the runtime a price it could not have known was
the fill, and the record's entry is stale in exactly the way the backtest is.

READ-ONLY.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src import bar_archive  # noqa: E402

LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}
TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 0.0000005}
TOL = 1.5  # ticks: treat anything closer than this as "this bar's price"


def load_bars(symbol: str, tf: str) -> pd.DataFrame:
    for cand in {tf, "1h", "15m", "5m"}:
        df = bar_archive.read_archive(symbol, cand)
        if df is not None and not df.empty:
            return df
    return pd.DataFrame()


def main() -> None:
    conn = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    df = pd.read_sql_query(
        """
        SELECT t.id tid, t.symbol, t.strategy_id,
               a.strategy_family, a.params_json,
               s.ts_utc sig_ts, s.entry sig_entry,
               t.entry_fill, t.exit_reason, t.pnl_dollars
        FROM trades t
        JOIN signals s  ON s.id = t.signal_id
        LEFT JOIN active_strategies a ON a.id = t.strategy_id
        WHERE t.entry_fill IS NOT NULL AND s.entry IS NOT NULL
        ORDER BY t.opened_at
        """, conn)
    conn.close()
    df = df[df.symbol.isin(LEGAL)].copy()
    df["tf"] = df.params_json.fillna("{}").apply(
        lambda p: (str(__import__("json").loads(p or "{}").get("timeframe", "1h"))
                   .lower().replace(" ", "")))
    print(f"legal fills: {len(df)}  timeframes seen: {sorted(df.tf.unique())}\n")

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
        idx = b["ts"].searchsorted(ts, side="right") - 1  # last bar ts <= signal ts
        if idx < 0:
            continue
        tol = TOL * TICK[r.symbol]
        age = None
        matched = "none"
        # walk backwards up to 12 bars looking for a price match
        for back in range(0, min(13, idx + 1)):
            row = b.iloc[idx - back]
            for fld in ("close", "open", "high", "low"):
                v = float(row[fld])
                if abs(v - float(r.sig_entry)) <= tol:
                    age, matched = back, fld
                    break
            if age is not None:
                break
        rows.append({
            "tid": r.tid, "symbol": r.symbol, "tf": r.tf,
            "strategy_id": r.strategy_id, "strategy_family": r.strategy_family,
            "sig_ts": r.sig_ts, "sig_entry": r.sig_entry,
            "entry_fill": r.entry_fill, "age_bars": age, "matched": matched,
            "exit_reason": r.exit_reason, "pnl": r.pnl_dollars,
            "dec_bar_ts": str(b.iloc[idx]["ts"]), "dec_bar_close": float(b.iloc[idx]["close"]),
        })

    out = pd.DataFrame(rows)
    if out.empty:
        print("no archive coverage for any fill -- archive may be empty")
        return
    print("=" * 78)
    print("LIVE SIGNAL ENTRY AGE  (bars before the decision bar)")
    print("=" * 78)
    print(f"measured fills with archive coverage: {len(out)}/{len(df)}")
    n_age = out.age_bars.notna().sum()
    print(f"entry matched to a bar within 12 bars back: {n_age} "
          f"({100.0*n_age/len(out):.1f}%)")
    print()
    print("age_bars histogram:")
    h = out.age_bars.value_counts(dropna=False).sort_index()
    for k, v in h.items():
        lbl = "NO MATCH (>12 back)" if pd.isna(k) else f"{int(k)} bar(s) back"
        print(f"   {lbl:>22}: {v:>5}  ({100.0*v/len(out):>5.1f}%)")
    print()
    print(f"mean age (matched only): {out.age_bars.mean():.2f} bars   "
          f"median: {out.age_bars.median():.0f}")
    print()
    print("--- by timeframe ---")
    print(f"{'tf':>5} {'n':>5} {'%matched':>9} {'mean_age':>9} {'med_age':>8} "
          f"{'%age0':>7}")
    for tf, sub in out.groupby("tf"):
        m = sub.age_bars.notna()
        print(f"{tf:>5} {len(sub):>5} {100.0*m.mean():>8.1f}% "
              f"{(sub.age_bars[m].mean() if m.any() else float('nan')):>9.2f} "
              f"{(sub.age_bars[m].median() if m.any() else float('nan')):>8.0f} "
              f"{100.0*(sub.age_bars == 0).mean():>6.1f}%")
    print()
    print("--- by strategy_family (matched only, n>=8) ---")
    for fam, sub in out[out.age_bars.notna()].groupby("strategy_family"):
        if len(sub) < 8:
            continue
        print(f"  {str(fam)[:44]:<44} n={len(sub):>4} "
              f"med_age={sub.age_bars.median():>4.0f} "
              f"mean_age={sub.age_bars.mean():>6.2f} "
              f"%age0={100.0*(sub.age_bars==0).mean():>5.1f}%")
    print()

    # P&L split by staleness -- does the stale half behave differently?
    print("--- realised P&L by entry age ---")
    for lab, sub in (("age==0 (decision-time)", out[out.age_bars == 0]),
                     ("age>0 (STALE entry)", out[out.age_bars > 0]),
                     ("no match", out[out.age_bars.isna()])):
        if sub.empty:
            continue
        w = int((sub.pnl > 0).sum())
        l = int((sub.pnl <= 0).sum())
        pf = (sub.pnl[sub.pnl > 0].sum() / abs(sub.pnl[sub.pnl <= 0].sum())
              if abs(sub.pnl[sub.pnl <= 0].sum()) > 0 else float("nan"))
        print(f"  {lab:<24} n={len(sub):>4} {w:>3}W/{l:<3}L "
              f"WR={100.0*w/max(len(sub),1):>5.1f}%  ${sub.pnl.sum():>+10.2f}  "
              f"PF={pf:>6.3f}")
    print()
    print("Interpretation: age>0 means the runtime was told an entry price from")
    print("a bar that had already closed. The live order is a MARKET order, so")
    print("we do NOT get that price -- we get the market. The strategy's stop")
    print("and target are then re-anchored to whatever that market price was.")

    out.to_csv("outputs/task2867_live_stale_entry_probe_20261023.csv", index=False)
    print("\nwrote outputs/task2867_live_stale_entry_probe_20261023.csv")


if __name__ == "__main__":
    main()