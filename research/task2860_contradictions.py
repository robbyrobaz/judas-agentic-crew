"""Task #2860 part B+E — backtest-vs-shadow contradictions and zero-signal audit."""
import sqlite3, json
import pandas as pd
import numpy as np
from datetime import timezone, timedelta

con = sqlite3.connect("judas_crew.db")
H = pd.read_parquet("research/task2860_shadow_honest.parquet")
L = pd.read_parquet("research/task2860_shadow_limit.parquet")
R = pd.read_parquet("research/task2860_shadow_retro.parquet")


def stats(d, sid):
    x = d[d.strategy_id == sid]
    if len(x) == 0:
        return None
    w, l = x[x.res == "WIN"], x[x.res == "LOSS"]
    return dict(n=len(x), W=len(w), L=len(l),
                PF=(w.r.sum() / -l.r.sum()) if len(l) else np.nan,
                netR=x.r.sum(), dollars=x.dollars.sum(), sess_=x.sess.mode()[0])


FOCUS = {4634: "MNQ 1h SB csid229 (+$31,064 BT envelope, PF 5.45 n=94, DELETED)",
         4693: "MNQ 5m ict_mitigation_block csid253 (PF 5.48 n=145, DELETED)",
         4995: "MGC ny_am_judas_ifvg 5m (PF 2.05 n=217 +$3,134, promoted today)",
         4994: "MNQ 15m csid434 (new hourly-bias/FVG wave, promoted today)",
         4993: "MGC 5m csid433 (new hourly-bias/FVG wave, promoted today)",
         4992: "MCL 15m csid435 (new hourly-bias/FVG wave, promoted today)"}

print("=" * 118)
print("(B) BACKTEST-vs-SHADOW CONTRADICTIONS — the six rows the operator named")
print("=" * 118)
rows = []
for sid, desc in FOCUS.items():
    h, l, r = stats(H, sid), stats(L, sid), stats(R, sid)
    st = con.execute("SELECT state, symbol, strategy_family, metrics_json FROM active_strategies WHERE id=?",
                     (sid,)).fetchone()
    print(f"\n#{sid}  {desc}")
    print(f"    registry: state={st[0] if st else 'ABSENT (deleted)'} sym={st[1] if st else '-'}"
          f" fam={st[2] if st else '-'}")
    if st and st[3]:
        m = json.loads(st[3])
        print(f"    stored BT metrics: { {k: m[k] for k in list(m)[:12] } }")
    for tag, s in (("HONEST(next-open)", h), ("LIMIT(entry-as-limit)", l), ("RETRO(backtest conv.)", r)):
        if s:
            print(f"    {tag:<24} n={s['n']:>3}  W={s['W']:>3} L={s['L']:>3}  "
                  f"PF={s['PF']:>7.3f}  netR={s['netR']:>8.2f}  net$={s['dollars']:>12,.0f}")
        else:
            print(f"    {tag:<24} ZERO SIGNALS IN WINDOW")
    rows.append(dict(sid=sid, n=h["n"] if h else 0,
                     honestPF=h["PF"] if h else np.nan, limitPF=l["PF"] if l else np.nan,
                     retroPF=r["PF"] if r else np.nan))

# ---- contradiction list: every strategy with a stored BT PF but shadow PF < 0.8
act = pd.read_sql("SELECT id, symbol, strategy_family, version, state, metrics_json, "
                  "params_json, activated_at_utc FROM active_strategies WHERE state='active'", con)
print(f"\nactive_strategies state='active' rows: {len(act)}")
print("\n" + "=" * 118)
print("(B2) SYSTEMATIC CONTRADICTION SWEEP — actives whose stored BT PF > 1.3 but HONEST shadow PF < 0.8")
print("=" * 118)


def bt_pf(mj):
    if not mj:
        return np.nan, np.nan
    try:
        m = json.loads(mj)
    except Exception:
        return np.nan, np.nan
    pf = m.get("profit_factor", m.get("pf"))
    n = m.get("n_trades", m.get("trades", m.get("n")))
    try:
        return float(pf), float(n)
    except (TypeError, ValueError):
        return np.nan, np.nan


hs = H.groupby("strategy_id").apply(
    lambda x: pd.Series({"sh_n": len(x),
                         "sh_PF": x.loc[x.res == "WIN", "r"].sum() / -x.loc[x.res == "LOSS", "r"].sum()
                         if (x.res == "LOSS").any() else np.nan,
                         "sh_$": x.dollars.sum()}), include_groups=False)
ls = L.groupby("strategy_id").size().rename("sh_lim_n")
m = act.join(hs, on="id").join(ls, on="id")
m[["bt_PF", "bt_n"]] = m.metrics_json.apply(lambda x: pd.Series(bt_pf(x)))
m[["sh_n", "sh_PF", "sh_$", "sh_lim_n"]] = m[["sh_n", "sh_PF", "sh_$", "sh_lim_n"]].fillna(0)
m["gap"] = m.bt_PF - m.sh_PF
bad = m[(m.bt_PF > 1.3) & (m.sh_n >= 20) & (m.sh_PF < 0.8)].sort_values("gap", ascending=False)
print(f"actives with a stored BT PF and >=20 shadow signals: {int(((m.bt_PF>1.3)&(m.sh_n>=20)).sum())}")
print(f"of those, shadow PF < 0.8 (DECAY / contradiction): {len(bad)}")
print(bad[["id", "symbol", "strategy_family", "bt_PF", "bt_n", "sh_n", "sh_PF", "sh_$", "gap"]]
      .head(30).to_string(index=False))

print("\n" + "=" * 118)
print("(E) ZERO-SIGNAL AUDIT — actives with ZERO signals 2026-10-12..2026-10-21")
print("=" * 118)
z = m[m.sh_n == 0]
print(f"{len(z)} of {len(m)} actives produced 0 signals in 10 days")
print(z.groupby("symbol").size().to_string())
print("\nby family (top 20):")
print(z.strategy_family.value_counts().head(20).to_string())
print("\nstranded by symbol+family:")
print(z[["id", "symbol", "strategy_family", "version", "activated_at_utc"]].head(40).to_string(index=False))

print("\n=== signal-rate distribution across the 175 actives ===")
print(m.sh_n.describe().to_string())
print("\nactives by signal bucket:")
bins = [-1, 0, 5, 20, 50, 100, 10**9]
lbl = ["0 (ZOMBIE)", "1-5", "6-20 (insufficient)", "21-50", "51-100", ">100"]
print(pd.cut(m.sh_n, bins=bins, labels=lbl).value_counts().reindex(lbl).to_string())

m.to_csv("research/task2860_actives_vs_shadow.csv", index=False)
bad.to_csv("research/task2860_contradictions.csv", index=False)
con.close()
print("\nwrote research/task2860_actives_vs_shadow.csv, research/task2860_contradictions.csv")