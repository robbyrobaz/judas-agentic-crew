"""TASK_2867_LEG_ANCHOR_AUDIT -- is the LIVE record contaminated in the
mirror direction of the backtest defect?

THE SETUP
---------
Finding 07f3a4c2 proved the BACKTEST fills at the strategy's returned `entry`,
which can be a bar that already closed -> every PF is inflated.

This script asks the opposite question about the LIVE path, and finds a
second, independent defect:

  src/broker/ninjatrader.py:915-928  `place_bracket` RE-ANCHORS stop/target
      to the ACTUAL NT fill (stop_dist preserved, measured from the fill).
  src/portfolio_runtime.py:930-938   `_save_signal_and_trade` writes
      `fire.stop` / `fire.target` into `trades.stop_price` /
      `trades.target_price` -- the SIGNAL-anchored levels.

So the levels we recorded are not the levels the broker had working.

DIRECTION OF ERROR (long, intended stop dist d, target dist k*d):
    signal   E,  S = E-d,   T = E+k*d
    fill     F  = E + delta
    working  S_w = F-d  = S+delta,   T_w = F+k*d = T+delta
    recorded S_r = S,                 T_r = T
    => recorded risk   = F - S   = d + delta   (vs working d)
    => recorded reward = T - F   = k*d - delta (vs working k*d)

delta < 0 (favourable slippage) is the dangerous case: the recorded stop sits
ABOVE the working stop, so a DB-driven exit resolves on a level the broker was
never resting at.

`pnl_dollars` is safe for the 692 stop/target exits (both legs are real NT
fills). What is NOT safe is every R-multiple, risk figure and bar-resolved
exit recomputed from `trades.stop_price` / `trades.target_price`.

READ-ONLY. sqlite mode=ro. Legal symbols only.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

DB = "judas_crew.db"
LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}
BANNED = {"DX", "MET", "MBT"}

SPEC = {
    "MGC": (0.10, 1.0),
    "MNQ": (0.25, 0.5),
    "MCL": (0.01, 1.0),
    "ZF": (0.0078125, 7.8125),
    "6J": (0.0000005, 6.25),
}

REAL_EXIT = {"stop", "target"}


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        """
        SELECT t.id AS trade_id, t.strategy_id, t.symbol, t.direction, t.qty,
               t.entry_fill, t.stop_price, t.target_price, t.exit_fill,
               t.pnl_dollars, t.exit_reason, t.opened_at, t.closed_at,
               s.entry AS sig_entry, s.stop AS sig_stop, s.target AS sig_target
        FROM trades t JOIN signals s ON s.id = t.signal_id
        WHERE t.entry_fill IS NOT NULL AND s.entry IS NOT NULL
        ORDER BY t.opened_at
        """,
        conn,
    )
    conn.close()

    n_all = len(df)
    df = df[df.symbol.isin(LEGAL)].copy()
    dropped = n_all - len(df)
    print(f"rows joined: {n_all}  |  legal-symbol rows: {len(df)}  |  banned dropped: {dropped}")
    print()

    # --- (1) did the signal's stop/target get written through verbatim? -----
    same_stop = np.isclose(df.stop_price, df.sig_stop, rtol=0, atol=1e-9)
    same_tgt = np.isclose(df.target_price, df.sig_target, rtol=0, atol=1e-9)
    print("=" * 78)
    print("(1) DID trades.stop_price/target_price = THE SIGNAL LEVELS?")
    print("=" * 78)
    print(f"stop_price  == signals.stop : {int(same_stop.sum())}/{len(df)}  "
          f"({100.0*same_stop.mean():.1f}%)")
    print(f"target_price== signals.target: {int(same_tgt.sum())}/{len(df)}  "
          f"({100.0*same_tgt.mean():.1f}%)")
    print("-> the DB records the SIGNAL levels verbatim, while the broker held")
    print("   FILL-ANCHORED levels. The two are not the same object.")
    print()

    # --- (2) how far did the working legs actually sit from the recorded? ---
    df["delta"] = df.entry_fill - df.sig_entry           # fill vs signal price
    d_long = df.direction.str.lower() == "long"
    df["stop_dist"] = (df.sig_entry - df.stop_price).where(d_long,
                                                           (df.stop_price - df.sig_entry)).abs()
    df["tgt_dist"] = (df.target_price - df.sig_entry).where(d_long,
                                                            (df.sig_entry - df.target_price)).abs()
    df = df[(df.stop_dist > 0) & df.tgt_dist.notna()].copy()
    # working (fill-anchored) levels, per ninjatrader.place_bracket
    df["stop_work"] = np.where(d_long, df.entry_fill - df.stop_dist,
                               df.entry_fill + df.stop_dist)
    df["tgt_work"] = np.where(d_long, df.entry_fill + df.tgt_dist,
                              df.entry_fill - df.tgt_dist)
    df["stop_drift_ticks"] = (df.stop_work - df.stop_price).abs() / df.symbol.map(
        lambda s: SPEC[s][0])
    df["tgt_drift_ticks"] = (df.tgt_work - df.target_price).abs() / df.symbol.map(
        lambda s: SPEC[s][0])

    print("=" * 78)
    print("(2) STOP/TARGET DRIFT: recorded level vs the level the broker held")
    print("=" * 78)
    print(f"{'sym':>5} {'n':>5} {'med_dticks':>10} {'p90_dticks':>10} {'max_ticks':>10} "
          f"{'%>1tick':>8}")
    for sym, sub in df.groupby("symbol"):
        s = sub.stop_drift_ticks
        print(f"{sym:>5} {len(sub):>5} {s.median():>10.1f} {s.quantile(.9):>10.1f} "
              f"{s.max():>10.1f} {100.0*(s > 1).mean():>7.1f}%")
    print()
    print("Any non-zero drift means the recorded leg is not the working leg.")
    print()

    # --- (3) what it does to the R multiple -------------------------------
    d = df[df.exit_reason.isin(REAL_EXIT) & df.exit_fill.notna()].copy()
    dpp = d.symbol.map(lambda s: SPEC[s][1] / SPEC[s][0])
    d["risk_rec"] = (d.entry_fill - d.stop_price).where(
        d.direction.str.lower() == "long", (d.stop_price - d.entry_fill)).abs() * dpp
    d["risk_work"] = (d.entry_fill - d.stop_work).where(
        d.direction.str.lower() == "long", (d.stop_work - d.entry_fill)).abs() * dpp
    ok = (d.risk_rec > 0) & (d.risk_work > 0)
    d = d[ok].copy()
    d["realised_R"] = d.pnl_dollars / (d.risk_work * d.qty)
    d["recorded_R"] = d.pnl_dollars / (d.risk_rec * d.qty)
    d["R_gap"] = d.realised_R - d.recorded_R

    print("=" * 78)
    print("(3) REALISED R: using the WORKING risk vs the RECORDED risk")
    print("    (both use the same real NT entry+exit fills; only the risk")
    print("     denominator differs -- so any gap is purely a recording error)")
    print("=" * 78)
    print(f"n real-exit trades: {len(d)}   span "
          f"{d.opened_at.min()} -> {d.closed_at.max()}")
    print(f"{'sym':>5} {'n':>5} {'med|R_rec|':>11} {'med|R_work|':>12} "
          f"{'med_gap':>9} {'p90_gap':>9}")
    for sym, sub in d.groupby("symbol"):
        print(f"{sym:>5} {len(sub):>5} {sub.recorded_R.abs().median():>11.3f} "
              f"{sub.realised_R.abs().median():>12.3f} {sub.R_gap.median():>9.4f} "
              f"{sub.R_gap.quantile(.9):>9.4f}")
    print()
    print(f"ALL          median |R_work| - |R_recorded| = "
          f"{d.realised_R.abs().median() - d.recorded_R.abs().median():+.4f} R")
    print(f"ALL          mean signed R_gap (work - rec)  = {d.R_gap.mean():+.4f} R")
    print()

    # --- (4) the two earners ------------------------------------------------
    print("=" * 78)
    print("(4) THE TWO BROKER-CONFIRMED EARNERS")
    print("=" * 78)
    for aid in (4599, 4678):
        sub = d[d.strategy_id == aid]
        if sub.empty:
            print(f"#{aid}: no real-exit trades in the legal-symbol set")
            continue
        g = sub.groupby("strategy_id").agg(
            n=("trade_id", "size"), w=("pnl_dollars", lambda s: int((s > 0).sum())),
            l=("pnl_dollars", lambda s: int((s <= 0).sum())),
            usd=("pnl_dollars", "sum"),
            pf=("pnl_dollars", lambda s: (s[s > 0].sum() / abs(s[s <= 0].sum())
                                          if abs(s[s <= 0].sum()) > 0 else np.nan)),
            wr=("pnl_dollars", lambda s: 100.0 * (s > 0).mean()),
            medR=("realised_R", lambda s: s.abs().median()),
        )
        span = (pd.to_datetime(sub.closed_at).max() - pd.to_datetime(sub.opened_at).min()).days
        r = g.iloc[0]
        print(f"#{aid} sym={sub.symbol.unique().tolist()} n={int(r.n)} "
              f"{int(r.w)}W/{int(r.l)}L  WR={r.wr:.1f}%  ${r.usd:+.2f}  "
              f"PF={r.pf:.3f}  med|R_work|={r.medR:.3f}  span={span}d")
    print()

    # --- (5) the favourable-slippage hazard --------------------------------
    print("=" * 78)
    print("(5) DIRECTION OF THE HAZARD")
    print("=" * 78)
    fav = d[d.delta < 0] if "delta" in d else None
    dfav = df[(df.direction.str.lower() == "long") & (df.delta < 0) | (df.direction.str.lower() == "short") & (df.delta > 0)]
    print(f"favourable-slippage fills (filled BETTER than the signal said): "
          f"{len(dfav)}/{len(df)} ({100.0*len(dfav)/len(df):.1f}%)")
    print("For those the RECORDED stop sits on the wrong side of the WORKING")
    print("stop -> any exit resolved from trades.stop_price is not the exit")
    print("the broker could have produced.")
    adverse = df[(df.direction.str.lower() == "long") & (df.delta > 0) | (df.direction.str.lower() == "short") & (df.delta < 0)]
    print(f"adverse-slippage fills: {len(adverse)}/{len(df)} ({100.0*len(adverse)/len(df):.1f}%)")
    print()

    df.to_csv("outputs/task2867_leg_anchor_audit_20261023.csv", index=False)
    print("wrote outputs/task2867_leg_anchor_audit_20261023.csv")


if __name__ == "__main__":
    main()