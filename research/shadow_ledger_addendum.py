#!/usr/bin/env python
"""
SHADOW LEDGER ADDENDUM — family-level rollup, retirement-cannibalisation quantification,
and the post-reset cap-allocation table.  Reads the CSVs produced by
shadow_ledger_2026_10_12_to_20.py.  Read-only.
"""
import sqlite3

import numpy as np
import pandas as pd

pd.set_option("display.width", 320)
pd.set_option("display.max_rows", 600)
pd.set_option("display.max_columns", 40)

df = pd.read_csv("research/shadow_signals.csv")
con = sqlite3.connect("judas_crew.db")
acts = pd.read_sql("SELECT id AS strategy_id, symbol AS act_symbol,"
                   " strategy_family AS act_family, state AS act_state,"
                   " activated_at_utc, deactivated_at_utc"
                   " FROM active_strategies", con)
con.close()
df = df.merge(acts, on="strategy_id", how="left")
per = pd.read_csv("research/shadow_per_strategy.csv")
cl = df[df["out_p"].isin(["WIN", "LOSS"])].copy()


def agg(g):
    n = len(g)
    w = g[g["out_p"] == "WIN"]["usd_p"].sum()
    l = -g[g["out_p"] == "LOSS"]["usd_p"].sum()
    return pd.Series(dict(n_sig=len(g), n_closed=n,
                          wr=(g["out_p"] == "WIN").mean() if n else float("nan"),
                          pf=(w / l) if (l > 0 and n) else float("nan"),
                          usd=g["usd_p"].sum(),
                          usd_per_sig=g["usd_p"].sum() / n if n else 0.0,
                          days=g["tradeday"].nunique(),
                          usd_per_cday=g["usd_p"].sum() / g["tradeday"].nunique()))


print("=" * 100)
print("PART 1 -- RETIREMENT CANNIBALISATION: strategies retired DURING the halt window")
print("=" * 100)
ret = cl[cl["act_state"] != "active"]
act = cl[cl["act_state"] == "active"]
print(f"signals from strategies that were RETIRED mid-window : n={len(ret):5d} "
      f"net=${ret['usd_p'].sum():>10,.0f}  PF="
      f"{ret[ret['out_p']=='WIN']['usd_p'].sum() / -ret[ret['out_p']=='LOSS']['usd_p'].sum():.3f}")
print(f"signals from strategies still ACTIVE at window end   : n={len(act):5d} "
      f"net=${act['usd_p'].sum():>10,.0f}  PF="
      f"{act[act['out_p']=='WIN']['usd_p'].sum() / -act[act['out_p']=='LOSS']['usd_p'].sum():.3f}")
print(f"\n-> The stale-retirer removed {ret['strategy_id'].nunique()} strategies that were "
      f"shadow-positive over the same window.")
print("\nRetired mid-window, ranked by shadow $ (n_closed>=10):")
rr = (ret[ret["strategy_id"].map(cl["strategy_id"].value_counts()) >= 10]
      .groupby(["strategy_id", "act_symbol", "act_family"]).apply(
          agg, include_groups=False).reset_index().sort_values("usd", ascending=False))
print(rr.to_string(index=False))
print(f"\n  retired-cohort total shadow $ = ${rr['usd'].sum():,.0f} over "
      f"{len(rr)} strategies")

print("\n" + "=" * 100)
print("PART 2 -- FAMILY ROLLUP (symbol x family, adverse, closed only)")
print("=" * 100)
fam = cl.groupby(["symbol", "strategy_family"]).apply(agg, include_groups=False).reset_index()
fam = fam[fam["n_closed"] >= 10].sort_values("usd", ascending=False)
print(fam.to_string(index=False))
fam.to_csv("research/shadow_family_rollup.csv", index=False)

print("\n" + "=" * 100)
print("PART 3 -- FAMILY ROLLUP, SYMBOL AGGREGATED (where should the 10 contracts go?)")
print("=" * 100)
famall = cl.groupby(["act_symbol", "act_family"]).apply(
    agg, include_groups=False).reset_index()
famall = famall[famall["n_closed"] >= 15].sort_values("usd", ascending=False)
print(famall.to_string(index=False))
famall.to_csv("research/shadow_family_rollup_allsym.csv", index=False)

print("\n" + "=" * 100)
print("PART 4 -- POST-RESET CAP ALLOCATION (10 contracts). Rank = shadow $ / contract-day")
print("=" * 100)
cand = per[(per["act_state"] == "active") & (per["n_closed"] >= 20)].copy()
cand = cand.sort_values("usd_per_contract_day", ascending=False)
cand["slot"] = range(1, len(cand) + 1)
cols = ["slot", "strategy_id", "act_symbol", "act_family", "n_sig", "n_closed", "wr",
        "pf", "pf_ub", "usd", "usd_per_sig", "usd_per_contract_day", "bt_pf", "bt_n"]
print(cand[cols].head(30).to_string(index=False))
print(f"\n... {len(cand)} active strategies clear n_closed>=20. "
      f"{len(cand[cand.pf>=1.3])} have shadow PF>=1.3, "
      f"{len(cand[(cand.pf>=1.0)&(cand.pf<1.3)])} are 1.0-1.3, "
      f"{len(cand[cand.pf<1.0])} are below 1.0.")
cand[cols].to_csv("research/shadow_cap_allocation.csv", index=False)

print("\n" + "=" * 100)
print("PART 5 -- SIGNAL VOLUME vs the 10-contract cap (how oversubscribed is the book?)")
print("=" * 100)
per_day = df.groupby(["symbol", "tradeday"]).size().unstack(fill_value=0)
print(per_day.to_string())
print(f"\ntotal closed trades over 9 sessions = {len(cl)}  "
      f"=> {len(cl)/9:.0f}/day book-wide vs a 10-contract cap.")
print("Every signal above is a HYPOTHETICAL. At most 10 could ever be live at once.")
best10 = per[(per.act_state == "active") & (per.n_closed >= 20)].nlargest(
    10, "usd_per_contract_day")
print(f"\nTop-10 active strategies by shadow $/contract-day would have produced "
      f"${best10['usd_per_contract_day'].sum():,.0f} over 9 sessions "
      f"(~${best10['usd_per_contract_day'].sum()/9:,.0f}/day) -- vs the whole book's "
      f"${cl['usd_p'].sum():,.0f}.")

print("\n" + "=" * 100)
print("PART 6 -- DIRECTION / QUALITY cross-check (does the edge live anywhere specific?)")
print("=" * 100)
print(cl.groupby(["symbol", "direction"]).apply(agg, include_groups=False).to_string())

# quality_score is NULL on 100% of rows in this window (verified against the
# signals table: n=8333, COUNT(quality_score)=0) -- the SetupEvaluator never
# populated it. Degrade gracefully instead of crashing the whole analysis.
if cl["quality_score"].notna().sum() == 0:
    print("\n[quality_score unavailable: NULL on 100% of rows -> bucketing skipped]")
else:
    q = cl.copy()
    q["qbin"] = pd.qcut(q["quality_score"].rank(method="first"), 5,
                        labels=["q1_worst", "q2", "q3", "q4", "q5_best"])
    print()
    print(q.groupby("qbin", observed=True).apply(agg, include_groups=False).to_string())
print("\nRR (target distance / stop distance) buckets:")
rr = cl.copy()
rr["rrb"] = pd.cut((rr["target"] - rr["entry"]).abs() / rr["risk_pts"],
                   [0, 0.9, 1.4, 1.9, 2.4, 99],
                   labels=["<0.9R", "1R", "1.5R", "2R", ">2.5R"])
print(rr.groupby("rrb", observed=True).apply(agg, include_groups=False).to_string())

print("\n" + "=" * 100)
print("PART 7 -- THE DECISIVE CUT: symbol x direction x family, with significance")
print("=" * 100)


def mean_ci(v):
    """95% CI on mean R-multiple (t-approx). Returns (mean, lo, hi)."""
    v = pd.Series(v).dropna()
    n = len(v)
    if n < 5:
        return float("nan"), float("nan"), float("nan")
    m = v.mean()
    se = v.std(ddof=1) / (n ** 0.5)
    return m, m - 1.96 * se, m + 1.96 * se


cells = (cl.groupby(["symbol", "direction", "strategy_family"])
         .agg(n_sig=("usd_p", "size"), n_closed=("out_p", "size"),
              usd=("usd_p", "sum"), tot_r=("r_p", "sum"),
              wr=("out_p", lambda s: (s == "WIN").mean()),
              n_cdays=("tradeday", "nunique"))
         .reset_index())
cells["usd_per_cday"] = cells["usd"] / cells["n_cdays"]
# expectancy in R with a 95% CI -- computed per group over the per-trade R series
ci = (cl.groupby(["symbol", "direction", "strategy_family"])["r_p"]
      .apply(lambda s: pd.Series(mean_ci(s), index=["e_r", "lo", "hi"]))
      .unstack().reset_index())
cells = cells.merge(ci, on=["symbol", "direction", "strategy_family"], how="left")
cells["sig"] = np.where(cells["lo"] > 0, "REAL",
                        np.where(cells["hi"] < 0, "LOSS", "noise"))
cells = cells[cells["n_closed"] >= 20].sort_values("e_r", ascending=False)
cols7 = ["symbol", "direction", "strategy_family", "n_closed", "wr", "e_r",
         "lo", "hi", "usd", "usd_per_cday", "sig"]
print(cells[cols7].to_string(index=False))
cells[cols7].to_csv("research/shadow_direction_cells.csv", index=False)

print("\n--- SIGN-VERDICT SUMMARY (n_closed>=20) ---")
print(cells.groupby("sig").agg(cells=("sig", "size"), usd=("usd", "sum")).to_string())
print("\n--- PER-SYMBOL x DIRECTION verdict ---")
sv = (cl.groupby(["symbol", "direction"])
      .agg(n_closed=("out_p", "size"), usd=("usd_p", "sum")).reset_index())
sv = sv.merge(cl.groupby(["symbol", "direction"])["r_p"]
              .apply(lambda s: pd.Series(mean_ci(s), index=["e_r", "lo", "hi"]))
              .unstack().reset_index(),
              on=["symbol", "direction"], how="left")
sv["sig"] = np.where(sv["lo"] > 0, "REAL",
                     np.where(sv["hi"] < 0, "LOSS", "noise"))
print(sv.to_string(index=False))
sv.to_csv("research/shadow_symbol_direction.csv", index=False)

print("\n--- HOW MUCH OF THE BOOK IS IN REAL CELLS? ---")
tot = cl["usd_p"].sum()
real = cells[cells["sig"] == "REAL"]["usd"].sum()
print(f"whole book shadow $      = ${tot:,.0f}")
print(f"sum over REAL cells only = ${real:,.0f}")
print(f"share of book $ in cells with CI(expectancy) > 0 = {real/tot:.1%}")
print("\n--- CONTRACT-OCCUPANCY IMPLICATION ---")
print("The 10-contract cap is filled book-wide, uniformly. If the edge is only")
print("in a subset of (symbol,direction,family) cells, then the cap is being")
print("spent on LOSING cells. Ranked REAL cells by $ per contract-day:")
rr2 = cells[cells["sig"] == "REAL"].nlargest(12, "usd_per_cday")
print(rr2[["symbol", "direction", "strategy_family", "n_closed", "e_r",
          "usd_per_cday"]].to_string(index=False))

print("\n" + "=" * 100)
print("PART 8 -- OUT-OF-SAMPLE CHECK (is the cell ranking real, or data-mined?)")
print("=" * 100)
# Rank cells on the FIRST half of the window, then score that frozen ranking on
# the SECOND half. A ranking that only works in-sample is a data-mined artifact.
days = sorted(cl["tradeday"].unique())
half = len(days) // 2
d_tr, d_te = days[:half], days[half:]
print(f"train days {d_tr[0]}..{d_tr[-1]}   test days {d_te[0]}..{d_te[-1]}")

KEY = ["symbol", "direction", "strategy_family"]


def cells_for(frame):
    return (frame.groupby(KEY)
            .agg(n_closed=("out_p", "size"), usd=("usd_p", "sum"),
                 tot_r=("r_p", "sum")).reset_index())


tr = cells_for(cl[cl["tradeday"].isin(d_tr)])
te = cells_for(cl[cl["tradeday"].isin(d_te)])
# minimum evidence in train to be eligible for ranking at all
tr = tr[tr["n_closed"] >= 15].nlargest(15, "usd")
print(f"\ntop-15 cells by train shadow $  (n={len(tr)}):")
print(tr.to_string(index=False))

m = tr.merge(te, on=KEY, how="left", suffixes=("_tr", "_te"))
m["usd_te"] = m["usd_te"].fillna(0.0)
m["n_te"] = m["n_closed_te"].fillna(0)
hit = (m["usd_te"] > 0).mean()
print(f"\ntest-window sign agreement: {hit:.1%} of frozen top-15 cells still positive"
      f"   (train was 100% by construction)")
print(f"train $ on those cells = ${m['usd_tr'].sum():,.0f}   "
      f"test $ on the SAME cells = ${m['usd_te'].sum():,.0f}")
print(f"degradation = {m['usd_te'].sum()/m['usd_tr'].sum()-1:+.1%}")

# correlation of per-cell train vs test expectancy (rank correlation, robust)
j = (tr[KEY + ["tot_r", "n_closed"]]
     .rename(columns={"tot_r": "r_tr", "n_closed": "n_tr"})
     .merge(te[KEY + ["tot_r", "n_closed"]]
            .rename(columns={"tot_r": "r_te", "n_closed": "n_te"}),
            on=KEY, how="inner"))
j["e_tr"] = j["r_tr"] / j["n_tr"]
j["e_te"] = j["r_te"] / j["n_te"]
if len(j) >= 3:
    from scipy.stats import spearmanr
    rho, pv = spearmanr(j["e_tr"], j["e_te"])
    print(f"\nSpearman rank corr of per-cell expectancy, train vs test "
          f"(n={len(j)} overlapping cells): rho={rho:+.3f}  p={pv:.4f}")
    print("  rho > 0 with p<0.05  =>  the cell structure REPLICATES, ranking is real")
    print("  rho ~ 0                =>  the ranking is a data-mined artifact")
j.to_csv("research/shadow_oos_cells.csv", index=False)