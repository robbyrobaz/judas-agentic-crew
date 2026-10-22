#!/usr/bin/env python
"""
SHADOW LEDGER — counterfactual P&L of the SKIP signals emitted 2026-10-12 -> 2026-10-21
while SimJudasFutures was HALTED on CUSHION_EXHAUSTED.

READ-ONLY. Touches nothing but cache_1h/*.parquet and SELECTs on judas_crew.db.

--------------------------------------------------------------------------- ASSUMPTIONS
PRICE SCALE NOTE (verified this cycle, do not skip):
  * 6J is stored/handled on the INVERSE scale (v = USD per JPY ~ 0.00634, i.e. 1/157.7).
    CME 6J contract = JPY 12,500,000, so $ per unit of the stored price = 12,500,000.
    (Naive "$12,500/point" is wrong by ~24x and inverts every 6J number.)
  * ZF (5-yr T-Note): $100,000 face -> $1,000 per full point; quoted in 1/64s, tick 0.015625.
  * MGC $10/pt (tick .10), MNQ $2/pt (tick .25), MCL $10/pt (tick .01).

FILL / RESOLUTION (deliberately adverse):
  * Fill at the signal's `entry` price on the signal bar.  If that bar OPENS through
    entry (long: open >= entry / short: open <= entry) we fill at the open instead --
    adverse slippage, never favourable.
  * Walk forward on 5m bars (finest cached).  First bar touching stop -> LOSS.
    First bar touching target -> WIN.
  * AMBIGUITY (a bar whose range spans BOTH stop and target): scored as a LOSS in the
    primary ledger, re-scored as a WIN for an upper bound.  Truth is between; both
    reported.  Never resolved favourably in the primary.
  * Exit slippage 1 tick adverse side.  Round-trip fee $2.00.
  * Max hold 72 x 5m bars (6h).  Unresolved -> OPEN, excluded from PF, counted.
  * Bars beyond the 2026-10-21T00:20Z cache edge are absent; those trades truncate,
    mechanically inflating OPEN. Counted, not hidden.

SYNTHETIC / bar-touch-reconciled evidence. NOT broker-confirmed P&L, NOT earnings.
Counterfactual ranking aid only.

FIX 2026-10-21 (Researcher): wins were scored at a flat +1.0R.  The signals carry
a real target, and the realised median payoff was +2.00R, so the old ledger threw
away ~1R of payoff per winner -- making `usd` a restatement of win rate and
invalidating the PART-4 contract-allocation ranking built on it.  Wins are now
scored at the actual capped reward:risk (clip 5R).  See the rr_act computation in
main().  Fee note: the flat $2.00 round-trip is material vs MCL/ZF risk sizing
(a median MCL winner nets ~$1.30 gross-of-fee on a 0.17pt stop) -- MCL results
are fee-dominated and should be read as such.
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

DB = "judas_crew.db"
WIN_FROM, WIN_TO = "2026-10-12", "2026-10-21"
MAX_HOLD = 72

CONTRACT = {
    "MGC": (10.0, 0.10),
    "MNQ": (2.0, 0.25),
    "MCL": (10.0, 0.01),
    "6J": (12_500_000.0, 2.0e-9),   # inverse-quoted; tick below float precision
    "ZF": (1000.0, 0.015625),
}
FEE = 2.00


def session_of(ts):
    h = ts.tz_convert("America/New_York").hour
    if h < 4:
        return "Asia_00_04ET"
    if h < 8:
        return "London_04_08ET"
    if h < 12:
        return "NY_AM_08_12ET"
    if h < 17:
        return "NY_PM_12_17ET"
    return "Post_17_20ET"


def load_bars(sym):
    df = pd.read_parquet(f"cache_1h/{sym}_5m.parquet")
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def resolve(direction, entry, stop, target, o, h, l, c, i0, ambig_wins):
    n = len(o)
    if i0 >= n:
        return ("NOBAR", 0.0)
    if direction == "long":
        if not (stop < entry < target):
            return ("BAD", 0.0)
    else:
        if not (stop > entry > target):
            return ("BAD", 0.0)

    fill = entry
    if direction == "long" and o[i0] >= entry:
        fill = o[i0]
    elif direction == "short" and o[i0] <= entry:
        fill = o[i0]
    risk = abs(fill - stop)
    if risk <= 0:
        return ("BAD", 0.0)

    last = min(i0 + MAX_HOLD, n - 1)
    for i in range(i0, last + 1):
        bh, bl = h[i], l[i]
        if direction == "long":
            s_hit, t_hit = (bl <= stop), (bh >= target)
        else:
            s_hit, t_hit = (bh >= stop), (bl <= target)
        if s_hit and t_hit:
            return ("WIN" if ambig_wins else "LOSS", 1.0 if ambig_wins else -1.0)
        if s_hit:
            return ("LOSS", -1.0)
        if t_hit:
            return ("WIN", 1.0)
    mtm = (c[last] - fill) if direction == "long" else (fill - c[last])
    return ("OPEN", mtm / risk)


def agg(g, usd, out):
    cl = g[g[out].isin(["WIN", "LOSS"])]
    n = len(cl)
    w = cl[cl[out] == "WIN"][usd].sum()
    l = -cl[cl[out] == "LOSS"][usd].sum()
    return pd.Series(dict(
        n_sig=len(g), n_closed=n,
        n_win=int((cl[out] == "WIN").sum()),
        n_loss=int((cl[out] == "LOSS").sum()),
        wr=(cl[out] == "WIN").mean() if n else np.nan,
        pf=(w / l) if (l > 0 and n) else np.nan,
        usd=cl[usd].sum() if n else 0.0,
        usd_per_sig=(cl[usd].sum() / n) if n else 0.0,
        days=g["tradeday"].nunique(),
        usd_per_contract_day=(cl[usd].sum() / g["tradeday"].nunique()) if n else 0.0,
        n_open=int((g[out] == "OPEN").sum()),
        avg_risk_pts=g["risk_pts"].mean()))


def main():
    con = sqlite3.connect(DB)
    sigs = pd.read_sql(
        f"SELECT id, ts_utc, symbol, direction, entry, stop, target, strategy_id,"
        f" strategy_family, quality_score FROM signals"
        f" WHERE ts_utc >= '{WIN_FROM}' AND ts_utc < '{WIN_TO}' ORDER BY ts_utc", con)
    acts = pd.read_sql(
        "SELECT id, symbol, strategy_family, version, state, activated_at_utc,"
        " deactivated_at_utc, metrics_json FROM active_strategies", con)
    con.close()

    bars, npv = {}, {}
    for s in CONTRACT:
        bars[s] = load_bars(s)
        npv[s] = dict(o=bars[s]["open"].to_numpy(dtype=float),
                      h=bars[s]["high"].to_numpy(dtype=float),
                      l=bars[s]["low"].to_numpy(dtype=float),
                      c=bars[s]["close"].to_numpy(dtype=float))

    rows = []
    for _, s in sigs.iterrows():
        sym = s["symbol"]
        if sym not in CONTRACT:
            continue
        ts = pd.Timestamp(s["ts_utc"])
        b = bars[sym]
        a = npv[sym]
        bts = b["ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        i0 = int(np.searchsorted(bts, np.datetime64(ts.tz_convert("UTC")
                                                   .tz_localize(None))))
        i0 = min(i0, len(b) - 1)
        o, h, l, c = a["o"], a["h"], a["l"], a["c"]
        per_pt, tick = CONTRACT[sym]
        risk_pts = abs(s["entry"] - s["stop"]) if s["entry"] is not None else np.nan
        # ACTUAL reward:risk this setup was offered.  Scoring every win at +1.0R
        # (the pre-2026-10-21 bug) discarded the median +1.00R of payoff, so the
        # usd column was a near-restatement of win rate and the "where do the 10
        # contracts go" ranking was built on it.  Cap the ratio so a
        # pathologically tight stop (observed max 65R) cannot dominate the book.
        rr_act = (abs(s["target"] - s["entry"]) / risk_pts
                  if (risk_pts and risk_pts > 0 and s["target"] is not None)
                  else np.nan)
        rr_act = float(np.clip(rr_act, 0.0, 5.0)) if np.isfinite(rr_act) else 1.0

        rec = dict(id=s["id"], ts_utc=s["ts_utc"], symbol=sym,
                   direction=s["direction"], strategy_id=s["strategy_id"],
                   strategy_family=s["strategy_family"],
                   quality_score=s["quality_score"], entry=s["entry"],
                   stop=s["stop"], target=s["target"],
                   session=session_of(ts), risk_pts=risk_pts)

        for tag, aw in (("p", False), ("u", True)):
            out, r = resolve(s["direction"], s["entry"], s["stop"], s["target"],
                             o, h, l, c, i0, aw)
            if out == "WIN":
                usd = (rr_act * risk_pts - tick) * per_pt - FEE
            elif out == "LOSS":
                usd = (-1.0 * risk_pts - tick) * per_pt - FEE
            elif out == "OPEN":
                usd = (r * risk_pts) * per_pt - FEE
            else:
                usd = 0.0
            rec[f"out_{tag}"] = out
            rec[f"usd_{tag}"] = usd
            rec[f"r_{tag}"] = r
        rows.append(rec)

    df = pd.DataFrame(rows)
    df["tradeday"] = (pd.to_datetime(df["ts_utc"])
                      .dt.tz_convert("America/New_York").dt.date.astype(str))
    df.to_csv("research/shadow_signals.csv", index=False)

    key = ["symbol", "ts_utc", "direction", "entry", "stop", "target"]
    df["is_dup"] = df.duplicated(subset=key, keep="first")
    uniq = df[~df["is_dup"]].copy()

    pd.set_option("display.width", 300)
    pd.set_option("display.max_rows", 600)
    pd.set_option("display.max_columns", 40)

    COLS = ["strategy_id", "act_symbol", "act_family", "act_state", "n_sig",
            "n_closed", "wr", "pf", "pf_ub", "usd", "usd_per_sig",
            "usd_per_contract_day", "n_open", "bt_pf", "bt_n"]

    print(f"signals={len(df)}  unique_setups={len(uniq)}  "
          f"dup_rate={1 - len(uniq)/len(df):.1%}")
    for tag, label in (("p", "ADVERSE  (ambiguous bar -> LOSS)"),
                       ("u", "UPPER BD (ambiguous bar -> WIN)")):
        a = agg(df, f"usd_{tag}", f"out_{tag}")
        au = agg(uniq, f"usd_{tag}", f"out_{tag}")
        print(f"\n=== BOOK-LEVEL {label} ===")
        print(f"  all signals : closed n={a.n_closed}  WR={a.wr:.3f}  "
              f"PF={a.pf:.3f}  net=${a.usd:,.0f}  open={a.n_open}")
        print(f"  unique setup: closed n={au.n_closed}  WR={au.wr:.3f}  "
              f"PF={au.pf:.3f}  net=${au.usd:,.0f}")
    amb = int((df["out_p"] != df["out_u"]).sum())
    print(f"\nambiguous 5m bars (span BOTH stop and target): {amb} = {amb/len(df):.1%}"
          "  <-- the entire PF band width")

    per = df.groupby("strategy_id").apply(
        lambda g: agg(g, "usd_p", "out_p"), include_groups=False).reset_index()
    pu = df.groupby("strategy_id").apply(
        lambda g: agg(g, "usd_u", "out_u"), include_groups=False
    ).reset_index()[["strategy_id", "pf", "usd"]].rename(
        columns={"pf": "pf_ub", "usd": "usd_ub"})
    per = per.merge(pu, on="strategy_id")
    lab = acts.rename(columns={"id": "strategy_id", "symbol": "act_symbol",
                               "strategy_family": "act_family", "version": "act_ver",
                               "state": "act_state", "activated_at_utc": "act_on",
                               "deactivated_at_utc": "act_off"})
    per = per.merge(lab[["strategy_id", "act_symbol", "act_family", "act_ver",
                         "act_state", "act_on", "act_off", "metrics_json"]],
                    on="strategy_id", how="left")
    per["bt_pf"] = pd.to_numeric(per["metrics_json"].apply(
        lambda s: (json.loads(s).get("profit_factor") if s else None)),
        errors="coerce")
    per["bt_n"] = pd.to_numeric(per["metrics_json"].apply(
        lambda s: ((json.loads(s).get("n_trades") or json.loads(s).get("trades"))
                   if s else None)), errors="coerce")
    per = per.drop(columns=["metrics_json"]).sort_values("usd", ascending=False)
    per.to_csv("research/shadow_per_strategy.csv", index=False)

    ok = per[per["n_closed"] >= 10]
    print(f"\n=== (B) PER-STRATEGY n_closed>=10 (n={len(ok)}) ===")
    print("--- TOP 25 by shadow $ ---")
    print(ok.head(25)[COLS].to_string(index=False))
    print("\n--- BOTTOM 25 by shadow $ ---")
    print(ok.tail(25)[COLS].to_string(index=False))

    t1 = per[(per.pf >= 1.3) & (per.n_closed >= 20)]
    t2 = per[(per.pf >= 1.0) & (per.pf < 1.3) & (per.n_closed >= 20)]
    t3 = per[(per.pf < 1.0) & (per.n_closed >= 20)]
    t4 = per[per.n_closed < 20]
    print(f"\nTIER1  PF>=1.3 & n>=20 : {len(t1)}")
    print(t1[COLS].to_string(index=False))
    print(f"\nTIER2  PF 1.0-1.3 & n>=20 : {len(t2)}")
    print(t2[COLS].to_string(index=False))
    print(f"\nTIER3  PF<1.0 & n>=20  (DECAY / HARMFUL) : {len(t3)}")
    print(t3[COLS].to_string(index=False))
    print(f"\nTIER4  n<20 (INSUFFICIENT) : {len(t4)}")

    print("\n=== (C1) BY SYMBOL (adverse) ===")
    print(df.groupby("symbol").apply(agg, "usd_p", "out_p").to_string())
    print("\n=== (C2) BY ET SESSION (adverse) ===")
    print(df.groupby("session").apply(agg, "usd_p", "out_p").to_string())
    print("\n=== (C3) SYMBOL x SESSION net $ (closed, adverse) ===")
    cl = df[df["out_p"].isin(["WIN", "LOSS"])]
    print(cl.pivot_table(index="session", columns="symbol", values="usd_p",
                         aggfunc=["sum", "count"]).to_string())
    print("\n=== (C4) SYMBOL x SESSION PF (closed, adverse) ===")
    pv = cl.pivot_table(index="session", columns="symbol", values="usd_p",
                        aggfunc=lambda x: 1.0)  # placeholder, replaced below
    for s_ in sorted(cl["symbol"].unique()):
        sub = cl[cl["symbol"] == s_]
        g = sub.groupby("session").apply(
            lambda x: x[x["out_p"] == "WIN"]["usd_p"].sum()
            / max(-x[x["out_p"] == "LOSS"]["usd_p"].sum(), 1e-9), include_groups=False)
        pv[s_] = g
    print(pv.round(2).to_string())

    print("\n=== (D) NEW-WAVE FAMILY SCAN ===")
    for pat in ["session_disp_fvg_pullback", "htf_1h_disp_5m_fvg_pullback",
                "fvg_pullback", "hourly"]:
        sel = per[per["act_family"].fillna("").str.contains(pat)]
        n_sig = int(sel["n_sig"].sum())
        print(f"  pattern '{pat}': {len(sel)} strategies, {n_sig} shadow signals")
        if len(sel):
            print(sel[COLS].to_string(index=False))

    print("\n=== (B3) BT-vs-SHADOW CONTRADICTIONS ===")
    c1 = per[(per.bt_pf.notna()) & (per.n_closed >= 10)
             & (per.bt_pf >= 1.5) & (per.pf < 0.8)]
    print(f"STRICT  BT_PF>=1.5 AND shadow_PF<0.8 (n>=10): {len(c1)}")
    print(c1[COLS].to_string(index=False))
    c2 = per[(per.bt_pf.notna()) & (per.n_closed >= 20)
             & (per.bt_pf >= 1.3) & (per.pf < 1.0)]
    print(f"\nWIDE    BT_PF>=1.3 AND shadow_PF<1.0 (n>=20): {len(c2)}")
    print(c2[COLS].to_string(index=False))
    c3 = per[(per.bt_pf.notna()) & (per.n_closed >= 20)
             & (per.bt_pf < 1.2) & (per.pf >= 1.3)]
    print(f"\nUPWARD  BT_PF<1.2 AND shadow_PF>=1.3 (n>=20): {len(c3)}")
    print(c3[COLS].to_string(index=False))
    per.to_csv("research/shadow_per_strategy.csv", index=False)

    signalled = set(df["strategy_id"])
    active = acts[acts["state"] == "active"]
    zero = active[~active["id"].isin(signalled)]
    print(f"\n=== (B4) ACTIVE STRATEGIES WITH ZERO SIGNALS IN WINDOW: {len(zero)}"
          f" of {len(active)} ===")
    print(zero[["id", "symbol", "strategy_family", "version",
                "activated_at_utc"]].to_string(index=False))
    notactive = per[per["act_state"] != "active"]
    print(f"\nsignalled but NOT active at end of window: {len(notactive)}")
    print(notactive[["strategy_id", "act_symbol", "act_family", "act_state",
                     "n_sig", "n_closed", "pf", "usd"]].to_string(index=False))

    for nm, t in (("tier1", t1), ("tier2", t2), ("tier3", t3)):
        t.to_csv(f"research/shadow_{nm}.csv", index=False)
    print("\nDONE")


if __name__ == "__main__":
    main()