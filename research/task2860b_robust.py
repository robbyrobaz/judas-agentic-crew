"""Task #2860 — robustness extras: offsetting intra-book pairs, R outliers, SE of mean R,
symbol x session grid, and the insufficient-sample bucket. READ-ONLY.
"""
import json, math
import sqlite3

import numpy as np
import pandas as pd

con = sqlite3.connect("judas_crew.db")
S = pd.read_parquet("research/task2860b_shadow_strict.parquet")
L = pd.read_parquet("research/task2860b_shadow_limit.parquet")
_sg = pd.read_sql("SELECT id, entry, stop, target FROM signals WHERE ts_utc>='2026-10-12' "
                  "AND ts_utc<'2026-10-22'", con).rename(columns={"id": "signal_id"})
for _d in (S, L):
    for _c in ("entry", "stop", "target"):
        if _c not in _d.columns:
            _d[_c] = _d.signal_id.map(_sg.set_index("signal_id")[_c])
S["intended_risk"] = (S.entry - S.stop).abs()
print(f"STRICT trades where the realised risk is <25% of the intended risk "
      f"(fill landed at/through the stop): {int((S.risk_pts < 0.25*S.intended_risk).sum())} "
      f"({(S.risk_pts < 0.25*S.intended_risk).mean():.2%})")

# ---------- 1. does the book contain offsetting pairs? (same symbol+ts, both directions)
k = S.groupby(["symbol", "ts_utc"]).direction.nunique()
both = k[k > 1]
print("=" * 100)
print("1) INTRA-BOOK OFFSETTING PAIRS — same symbol+timestamp, both long and short")
print("=" * 100)
print(f"symbol-timestamps carrying BOTH directions: {len(both)} of {S.groupby(['symbol','ts_utc']).ngroups}"
      f" ({len(both)/S.groupby(['symbol','ts_utc']).ngroups:.1%})")
sub = S.merge(both.rename("both").reset_index(), on=["symbol", "ts_utc"])
print(f"signal rows inside those pairs: {len(sub)} ({len(sub)/len(S):.1%} of the book)")
print(f"netR inside paired rows: {sub.r.sum():+.1f}   netR outside: {S[~S.set_index(['symbol','ts_utc']).index.isin(both.index)].r.sum():+.1f}")

# ---------- 2. R outliers
print("\n" + "=" * 100)
print("2) R-MULTIPLE OUTLIERS — the huge PFs are micro-risk artefacts, not edges")
print("=" * 100)
print(S.r.describe(percentiles=[.01, .5, .99]).to_string())
print(f"trades with |R| > 10 : {int((S.r.abs() > 10).sum())}   with R > 50 : {int((S.r > 50).sum())}")
print("top 10 R trades:")
print(S.nlargest(10, "r")[["signal_id", "symbol", "strategy_id", "ts_utc", "direction",
                           "fill", "risk_pts", "r", "dollars"]].to_string(index=False))
Sc = S.copy()
Sc["r_cap"] = Sc.r.clip(-10, 10)


def pf(d, col="r"):
    w = d.loc[d.res == "WIN", col].sum()
    l = -d.loc[d.res == "LOSS", col].sum()
    return w / l if l > 0 else np.nan


print(f"\nbook PF raw={pf(S):.3f}   PF with R capped at +-10 = {pf(Sc,'r_cap'):.3f}")
print(f"book netR raw={S.r.sum():.1f}  capped={Sc.r_cap.sum():.1f}")
print(f"risk_pts (points risked) median={S.risk_pts.median():.4f} p05={S.risk_pts.quantile(.05):.4f} "
      f"p95={S.risk_pts.quantile(.95):.4f}")

# ---------- 3. is the book distinguishable from zero?
print("\n" + "=" * 100)
print("3) STATISTICAL POWER — is the book distinguishable from a coin flip?")
print("=" * 100)
for tag, d in (("STRICT", S), ("LIMIT", L)):
    x = d.r.values
    se = x.std(ddof=1) / np.sqrt(len(x))
    print(f"{tag}: n={len(x)} meanR={x.mean():+.4f} SE={se:.4f} 95%CI=[{x.mean()-1.96*se:+.3f},"
          f"{x.mean()+1.96*se:+.3f}]  P(mean>0)={1-0.5*(1+math.erf(x.mean()/se/math.sqrt(2))):.3f}")
u = S.drop_duplicates(["symbol", "ts_utc", "direction", "entry", "stop", "target"])
x = u.r.values
se = x.std(ddof=1) / np.sqrt(len(x))
print(f"STRICT de-duplicated setups: n={len(x)} meanR={x.mean():+.4f} SE={se:.4f} "
      f"95%CI=[{x.mean()-1.96*se:+.3f},{x.mean()+1.96*se:+.3f}]")

# ---------- 4. symbol x session
print("\n" + "=" * 100)
print("4) STRICT netR / PF BY SYMBOL x SESSION")
print("=" * 100)
g = S.groupby(["symbol", "sess"]).apply(lambda x: pd.Series({
    "n": len(x), "W": (x.res == "WIN").sum(), "L": (x.res == "LOSS").sum(),
    "PF": pf(x), "netR": x.r.sum()}), include_groups=False)
print(g.to_string())

# ---------- 5. per-strategy table with capped PF + OPEN + LIMIT cross-check, top & bottom
act = pd.read_sql("SELECT id, symbol, strategy_family, version, params_json, metrics_json "
                  "FROM active_strategies WHERE state='active'", con)
S["r_cap"] = S.r.clip(-10, 10)
L["r_cap"] = L.r.clip(-10, 10)
t = S.groupby("strategy_id").apply(lambda x: pd.Series({
    "n": len(x), "W": (x.res == "WIN").sum(), "L": (x.res == "LOSS").sum(),
    "OPEN": (x.res == "OPEN").sum(), "PF": pf(x), "PF_cap": pf(x, "r_cap"),
    "netR": x.r.sum(), "netR_cap": x.r_cap.sum(), "$": x.dollars.sum(),
    "days": x.ts_utc.str[:10].nunique()}), include_groups=False)
tl = L.groupby("strategy_id").apply(lambda x: pd.Series({
    "lim_n": len(x), "lim_PF": pf(x, "r_cap"), "lim_netR": x.r_cap.sum()}), include_groups=False)
t = t.join(tl, how="left").join(act.set_index("id")[["symbol", "strategy_family"]], how="left")
t[["lim_n", "lim_PF", "lim_netR"]] = t[["lim_n", "lim_PF", "lim_netR"]].fillna(0)
t["per_contract_day"] = t["$"] / t.days
print("\n" + "=" * 100)
print("5) TOP 15 STRICT by $/contract-day (n>=20) — with LIMIT cross-check")
print("=" * 100)
c = t[t.n >= 20]
print(c.sort_values("per_contract_day", ascending=False).head(15)[
    ["symbol", "strategy_family", "n", "W", "L", "OPEN", "PF", "PF_cap", "netR_cap", "$",
     "days", "per_contract_day", "lim_n", "lim_PF", "lim_netR"]].to_string())
print("\nBOTTOM 10 (n>=20) — the ones the book is funding:")
print(c.sort_values("per_contract_day").head(10)[
    ["symbol", "strategy_family", "n", "W", "L", "OPEN", "PF", "netR_cap", "$", "per_contract_day"]].to_string())
ins = t[t.n < 20]
print(f"\nINSUFFICIENT (0-19 shadow signals): {len(ins)} actives / {int(ins.n.sum())} signals "
      f"-> grouped by symbol:")
print(ins.groupby("symbol").size().to_string())
t.to_csv("research/task2860b_strategy_table.csv")
con.close()
print("\nwrote research/task2860b_strategy_table.csv")