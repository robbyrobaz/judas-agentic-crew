"""LIVE_FILL_VS_SIGNAL_ENTRY_2026-10-23 -- task #2867(B)(4).

THE QUESTION
------------
Does the LIVE path submit an order priced from the strategy's returned
`entry` (a bar that may already have closed), or does it go to market and
re-anchor? Finding 07f3a4c2 proved the BACKTEST fills at the signal entry.
The live record is the only place we can test whether reality does the same.

THE TEST
--------
`signals.entry`   = what the strategy returned (the signal price)
`trades.entry_fill` = what NinjaTrader actually filled us at

If the live path were retro-filled (or limit-at-stale-price), these would be
IDENTICAL. Every divergence is market slippage the strategy did not get to
avoid. Direction of harm:

    long  : fill > sig_entry  -> ADVERSE (we paid up)
    short : fill < sig_entry  -> ADVERSE (we paid down)

READ-ONLY. sqlite mode=ro.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

DB = "judas_crew.db"
LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        """
        SELECT t.id AS trade_id, t.strategy_id, t.symbol, t.direction,
               t.qty, s.entry AS sig_entry, t.stop_price, t.target_price,
               t.entry_fill AS nt_fill, t.pnl_dollars, t.status,
               t.opened_at, t.closed_at, s.ts_utc AS sig_ts
        FROM trades t
        JOIN signals s ON s.id = t.signal_id
        WHERE s.entry IS NOT NULL AND t.entry_fill IS NOT NULL
        ORDER BY t.opened_at
        """,
        conn,
    )
    conn.close()
    banned = sorted(set(df.symbol) - LEGAL)
    if banned:
        print(f"[note] banned symbols present in history, EXCLUDED: {banned}")
        df = df[~df.symbol.isin(set(banned) - LEGAL)]

    d = df["direction"].str.lower()
    df["diff"] = df["nt_fill"] - df["sig_entry"]
    df["pct"] = 100.0 * df["diff"].abs() / df["sig_entry"].abs()
    df["signed_pct"] = np.where(
        d == "long", 100.0 * df["diff"] / df["sig_entry"].abs(),
        -100.0 * df["diff"] / df["sig_entry"].abs(),
    )  # + = adverse (paid up on a long, down on a short)
    df["adverse"] = df["signed_pct"] > 0

    print("=" * 78)
    print("LIVE FILL vs SIGNAL ENTRY  (all broker fills with a linked signal)")
    print("=" * 78)
    print(f"n fills joined        : {len(df)}")
    print(f"span                  : {df.opened_at.min()} -> {df.opened_at.max()}")
    exact = int((df["pct"] < 1e-9).sum())
    print(f"fills == signal entry : {exact}  ({100.0*exact/len(df):.1f}%)")
    print(f"fills != signal entry : {len(df)-exact}  ({100.0*(len(df)-exact)/len(df):.1f}%)")
    print()
    print(f"median |slippage|     : {df.pct.median():.4f}%")
    print(f"mean   |slippage|     : {df.pct.mean():.4f}%")
    print(f"p90    |slippage|     : {df.pct.quantile(0.90):.4f}%")
    print(f"max    |slippage|     : {df.pct.max():.4f}%")
    print(f"adverse fraction      : {100.0*df.adverse.mean():.1f}%  "
          f"({int(df.adverse.sum())}/{len(df)})")
    print(f"mean signed slippage  : {df.signed_pct.mean():+.4f}%  "
          "(+ = adverse, paid the wrong way on average)")
    print()

    print("--- by symbol ---")
    g = df.groupby("symbol")
    print(f"{'sym':>5} {'n':>4} {'med|%sl|':>9} {'mean|%sl|':>10} {'max|%sl|':>9} "
          f"{'adv%':>6} {'mean_signed%':>13}")
    for sym, sub in g:
        print(f"{sym:>5} {len(sub):>4} {sub.pct.median():>9.4f} {sub.pct.mean():>10.4f} "
              f"{sub.pct.max():>9.4f} {100.0*sub.adverse.mean():>6.1f} "
              f"{sub.signed_pct.mean():>+13.4f}")
    print()

    print("--- worst 10 adverse fills (points on MNQ/ZF are whole ticks) ---")
    worst = df.nlargest(10, "signed_pct")
    print(f"{'trd':>5} {'strat':>6} {'sym':>4} {'dir':>6} {'sig_entry':>12} "
          f"{'nt_fill':>12} {'signed%':>9}")
    for _, r in worst.iterrows():
        print(f"{r.trade_id:>5} {r.strategy_id:>6} {r.symbol:>4} {r.direction:>6} "
              f"{r.sig_entry:>12.5f} {r.nt_fill:>12.5f} {r.signed_pct:>+9.4f}")
    print()

    # THE 07f3a4c2 BRIDGE: does charging this real slippage to the backtest
    # erase the edge?  Use each symbol's mean ADVERSE slippage as a one-way
    # entry haircut and re-derive R on the recorded stop/target distances.
    print("=" * 78)
    print("BRIDGE: what the slippage costs, using each symbol's mean ADVERSE slip")
    print("=" * 78)
    print(f"{'sym':>5} {'n':>4} {'mean_adv%':>10} {'meanR_realised':>15} "
          f"{'meanR_after_slip':>18}")
    rows = []
    for sym, sub in df.groupby("symbol"):
        adv = float(sub.loc[sub.adverse, "signed_pct"].mean())  # already in pct
        if not np.isfinite(adv):
            adv = 0.0
        realised = []
        for _, r in sub.iterrows():
            if not r.stop_price or not r.target_price or not r.sig_entry:
                continue
            risk = abs(r.sig_entry - r.stop_price)
            if risk <= 0:
                continue
            rew = abs(r.target_price - r.sig_entry)
            realised.append(rew / risk)
        if not realised:
            continue
        mr = float(np.mean(realised))
        # entry moves adversely by adv%; R:R geometry preserved (broker
        # re-anchors distances), so the only loss is the slippage expressed
        # in R: adv% of notional converted using the stop distance.
        haircut_r = float(np.mean([
            (adv / 100.0) * abs(r.sig_entry) / abs(r.sig_entry - r.stop_price)
            for _, r in sub.iterrows()
            if r.stop_price and abs(r.sig_entry - r.stop_price) > 0
        ])) if adv > 0 else 0.0
        rows.append((sym, len(sub), adv, mr, mr - haircut_r))
    for sym, n, adv, mr, ma in rows:
        print(f"{sym:>5} {n:>4} {adv:>10.4f} {mr:>15.3f} {ma:>18.3f}")
    print()
    print("Read: 'meanR_realised' is the R:R the strategy ASKED for (target dist")
    print("/stop dist). 'meanR_after_slip' subtracts the adverse entry haircut in R")
    print("units. The broker re-anchors stop/target to the real fill, so geometry")
    print("survives -- but the entry itself is worse than the backtest assumed.")

    df.to_csv("outputs/live_fill_vs_signal_entry_20261023.csv", index=False)
    print("\nwrote outputs/live_fill_vs_signal_entry_20261023.csv")


if __name__ == "__main__":
    main()