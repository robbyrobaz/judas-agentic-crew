"""Task #2860 part B+D+E — contradictions, dedup-aware aggregates, cap-allocation shortlist,
zero-signal audit.  Reads the STRICT/HONEST/RETRO/LIMIT parquets from task2860_shadow_ledger_v2.
READ-ONLY.
"""
import json
import sqlite3

import numpy as np
import pandas as pd

con = sqlite3.connect("judas_crew.db")
S = pd.read_parquet("research/task2860b_shadow_strict.parquet")
H = pd.read_parquet("research/task2860b_shadow_honest.parquet")
R = pd.read_parquet("research/task2860b_shadow_retro.parquet")
L = pd.read_parquet("research/task2860b_shadow_limit.parquet")


def pf(d):
    w = d.loc[d.res == "WIN", "r"].sum()
    l = -d.loc[d.res == "LOSS", "r"].sum()
    return w / l if l > 0 else np.nan


def stats(d, sid):
    x = d[d.strategy_id == sid]
    if len(x) == 0:
        return None
    return dict(n=len(x), W=(x.res == "WIN").sum(), L=(x.res == "LOSS").sum(),
                OPEN=(x.res == "OPEN").sum(), PF=pf(x), netR=x.r.sum(),
                dollars=x.dollars.sum())


print("=" * 112)
print("(0) FILL-MODEL DELTA ON THE IDENTICAL 9,3xx SIGNALS — how much of the book is the fill?")
print("=" * 112)
for tag, d in (("STRICT  (first bar OPENING after the signal)", S),
               ("HONEST  (open of next bar after last closed bar)", H),
               ("RETRO   (signal's own entry, backtest convention)", R),
               ("LIMIT   (own entry as a resting limit)", L)):
    res = d[d.res != "OPEN"]
    print(f"{tag:<52} n={len(d):>5}  W={int((d.res=='WIN').sum()):>4} L={int((d.res=='LOSS').sum()):>4} "
          f"PF_all={pf(d):>6.3f}  PF_resolved={pf(res):>6.3f}  netR={d.r.sum():>9.1f}  net$={d.dollars.sum():>13,.0f}")
b = S.copy(); b["r_retro"] = R.set_index("signal_id").r.reindex(b.signal_id).values
print(f"\n  RETRO minus STRICT, same signals: netR {R.r.sum()-S.r.sum():+.0f}  "
      f"({(R.r.sum()-S.r.sum())/len(S):+.2f} R per signal of pure fill fiction)")
print(f"  signals whose own `entry` never trades back (LIMIT no-fill): {len(R)-len(L)} "
      f"({(len(R)-len(L))/len(R):.1%} of the book)")

# duplicate-setup audit: many actives are the same architecture on the same bar
_sg = pd.read_sql("SELECT id, entry, stop, target FROM signals WHERE ts_utc>='2026-10-12' "
                  "AND ts_utc<'2026-10-22'", con).rename(columns={"id": "signal_id"})
for _d in (S, H, R, L):
    for _c in ("entry", "stop", "target"):
        if _c not in _d.columns:
            _d[_c] = _d.signal_id.map(_sg.set_index("signal_id")[_c])
key = ["symbol", "ts_utc", "direction", "entry", "stop", "target"]
dup = S.duplicated(key, keep=False)
print(f"\n  signal rows: {len(S)}   DISTINCT market setups (sym,ts,dir,entry,stop,target): {S[key].drop_duplicates().shape[0]}")
u = S.drop_duplicates(key)
print(f"  de-duplicated SETUP-level STRICT: n={len(u)} PF={pf(u):.3f} netR={u.r.sum():.1f} net$={u.dollars.sum():,.0f}")
print("  => the 175 actives are NOT 175 independent bets; the book-level $ is a multiplicity artefact.")

print("\n" + "=" * 112)
print("(B) BACKTEST-vs-SHADOW CONTRADICTIONS — the rows the operator named")
print("=" * 112)
FOCUS = {4634: "MNQ 1h SB csid229 (BT PF 5.45 n=94 +$31,064, DELETED)",
         4693: "MNQ 5m ict_mitigation_block csid253 (BT PF 5.48 n=145, DELETED)",
         4995: "MGC ny_am_judas_ifvg 5m (BT PF 2.05 n=217 +$3,134, promoted 10-21)",
         4994: "MNQ 15m csid434 (new hourly-bias/FVG wave, promoted 10-21)",
         4993: "MGC 5m csid433 (new hourly-bias/FVG wave, promoted 10-21)",
         4992: "MCL 15m csid435 (new hourly-bias/FVG wave, promoted 10-21)"}
rows = []
for sid, desc in FOCUS.items():
    st = con.execute("SELECT state, symbol, strategy_family, version, params_json, metrics_json "
                     "FROM active_strategies WHERE id=?", (sid,)).fetchone()
    print(f"\n#{sid}  {desc}")
    if st is None:
        print("    registry: ROW ABSENT — no signals attributed to this id in window")
    else:
        p = json.loads(st[4]) if st[4] else {}
        m = json.loads(st[5]) if st[5] else {}
        print(f"    registry: state={st[0]} {st[1]} {st[2]} v{st[3]} tf={p.get('timeframe')} "
              f"csid={p.get('custom_strategy_id')}")
        print(f"    stored BT: PF={m.get('profit_factor')} n={m.get('n_trades')} "
              f"totPnl={m.get('total_pnl')} window_days={m.get('backtest_window_days')} "
              f"cost={m.get('cost_model')}")
    for tag, d in (("STRICT", S), ("HONEST", H), ("LIMIT", L), ("RETRO(backtest)", R)):
        v = stats(d, sid)
        if v:
            print(f"    {tag:<16} n={v['n']:>3} W={v['W']:>3} L={v['L']:>3} O={v['OPEN']:>3} "
                  f"PF={v['PF']:>7.3f} netR={v['netR']:>8.2f} net$={v['dollars']:>11,.0f}")
        else:
            print(f"    {tag:<16} ZERO SIGNALS IN WINDOW")
    sv = stats(S, sid) or dict(n=0, PF=np.nan, netR=np.nan, dollars=np.nan)
    rows.append(dict(sid=sid, sh_n=sv["n"], sh_PF=sv["PF"], sh_netR=sv["netR"], sh_d=sv["dollars"]))
print("\n" + "=" * 112)
print("(B2) SYSTEMATIC SWEEP — every ACTIVE with a stored BT PF and >=20 shadow signals")
print("=" * 112)


def bt_pf(mj):
    if not mj:
        return np.nan, np.nan
    try:
        m = json.loads(mj)
    except Exception:
        return np.nan, np.nan
    try:
        return float(m.get("profit_factor", m.get("pf"))), float(m.get("n_trades", m.get("trades", 0)))
    except (TypeError, ValueError):
        return np.nan, np.nan


act = pd.read_sql("SELECT id, symbol, strategy_family, version, state, metrics_json, params_json, "
                  "activated_at_utc FROM active_strategies WHERE state='active'", con)
act[["bt_PF", "bt_n"]] = act.metrics_json.apply(lambda x: pd.Series(bt_pf(x)))


def agg(d):
    return d.groupby("strategy_id").apply(lambda x: pd.Series({
        "sh_n": len(x), "sh_W": (x.res == "WIN").sum(), "sh_L": (x.res == "LOSS").sum(),
        "sh_PF": pf(x), "sh_netR": x.r.sum(), "sh_$": x.dollars.sum(),
        "days": x.ts_utc.str[:10].nunique()}), include_groups=False)


m = act.join(agg(S), on="id")
for c in ("sh_n", "sh_W", "sh_L", "sh_PF", "sh_netR", "sh_$", "days"):
    m[c] = m[c].fillna(0)
m["sh_PF"] = m.sh_PF.replace(0, np.nan)
m["gap"] = m.bt_PF - m.sh_PF
elig = m[(m.bt_PF > 1.3) & (m.sh_n >= 20)]
bad = m[(m.bt_PF > 1.3) & (m.sh_n >= 20) & (m.sh_PF < 0.8)].sort_values("gap", ascending=False)
print(f"actives with stored BT PF>1.3 AND >=20 shadow signals : {len(elig)}")
print(f"  ...of those, STRICT shadow PF < 0.8 (live decay)     : {len(bad)}")
print(f"  ...of those, STRICT shadow PF < 1.0                   : {int((elig.sh_PF < 1.0).sum())}")
print(f"  ...of those, STRICT shadow PF > 1.2 (edge survives)    : {int((elig.sh_PF > 1.2).sum())}")
cols = ["id", "symbol", "strategy_family", "bt_PF", "bt_n", "sh_n", "sh_W", "sh_L", "sh_PF", "sh_netR", "sh_$", "gap"]
print("\nworst contradictions (BT PF high, shadow PF low):")
print(bad.sort_values(["sh_PF", "gap"], ascending=[True, False])[cols].to_string(index=False))
print("\nedge survives (BT PF>1.3, shadow PF>1.2):")
print(elig[elig.sh_PF > 1.2].sort_values("sh_$", ascending=False)[cols].to_string(index=False))

print("\n" + "=" * 112)
print("(D) CAP-ALLOCATION SHORTLIST — 10 contracts post-reset, ranked by STRICT shadow_$ / contract-day")
print("=" * 112)
c = m[(m.sh_n >= 20) & (m.days > 0)].copy()
c["per_contract_day"] = c["sh_$"] / c.days
c["slot_key"] = c.params_json.apply(lambda x: (json.loads(x) or {}).get("slot_key"))
short = c.sort_values("per_contract_day", ascending=False)[
    ["id", "symbol", "strategy_family", "slot_key", "sh_n", "sh_W", "sh_L", "sh_PF",
     "sh_netR", "sh_$", "days", "per_contract_day", "bt_PF"]].head(15)
print(short.to_string(index=False))
ins = c[c.sh_n < 20]
print(f"\nINSUFFICIENT EVIDENCE, DO NOT GUESS: {len(ins)} actives with 0-19 shadow signals "
      f"({int(ins.sh_n.sum())} signals total)")
print(ins.groupby("symbol").size().to_string())

print("\n" + "=" * 112)
print("(E) ZERO-SIGNAL AUDIT — actives with ZERO signals 2026-10-12..2026-10-21")
print("=" * 112)
z = m[m.sh_n == 0]
print(f"{len(z)} of {len(m)} actives produced 0 signals in 10 days")
print(z.groupby("symbol").size().to_string())
print("\nby family:")
print(z.strategy_family.value_counts().head(25).to_string())
print("\nzero-signal actives that ARE retired vs still active:")
z2 = pd.read_sql("SELECT id, symbol, strategy_family, version, state FROM active_strategies", con)
zz = z2[(z2.state == "active") & z2.id.isin(z.id)]
print(f"still ACTIVE with zero signals: {len(zz)}")
print(zz[["id", "symbol", "strategy_family", "version"]].to_string(index=False))

m.to_csv("research/task2860b_actives_vs_shadow.csv", index=False)
bad.sort_values("gap", ascending=False).to_csv("research/task2860b_contradictions.csv", index=False)
con.close()
print("\nwrote research/task2860b_actives_vs_shadow.csv, research/task2860b_contradictions.csv")