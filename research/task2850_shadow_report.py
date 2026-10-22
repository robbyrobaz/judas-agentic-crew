"""Task #2850 part 2 -- aggregation + cap-allocation ranking for the shadow ledger.

Consumes research/task2850_shadow_ledger.json (SYNTHETIC bar-touch-reconciled
evidence) and joins the BT envelope stored on each active_strategies row.

READ-ONLY.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "judas_crew.db"
LEDGER = REPO / "research" / "task2850_shadow_ledger.json"


def pf_of(g: pd.DataFrame) -> float:
    w = g.loc[g.pnl > 0, "pnl"].sum()
    l = -g.loc[g.pnl < 0, "pnl"].sum()
    return float(w / l) if l > 0 else (0.0 if w == 0 else 999.0)


def main() -> None:
    t = pd.read_json(LEDGER)
    t = t[t.outcome.isin(["WIN", "LOSS", "OPEN"])].copy()
    t["ts_utc"] = pd.to_datetime(t["ts_utc"], utc=True, format="ISO8601")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    act = pd.read_sql("SELECT id, symbol, state, activated_at_utc, deactivated_at_utc, "
                      "params_json, metrics_json, source_candidate_id FROM active_strategies", con)
    cand = pd.read_sql("SELECT * FROM strategy_candidates WHERE id IN (7368,7369,7370,7371,7372)", con)
    con.close()
    pm = {int(r.id): json.loads(r.params_json or "{}") for r in act.itertuples()}
    mm = {int(r.id): json.loads(r.metrics_json or "{}") for r in act.itertuples()}
    st = {int(r.id): r.state for r in act.itertuples()}
    src = {int(r.id): r.source_candidate_id for r in act.itertuples()}
    for c in ("bt_pf", "bt_n", "bt_pnl", "bt_wr"):
        t[c] = np.nan
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return np.nan
    for sid in t.strategy_id.unique():
        m = mm.get(int(sid), {})
        pf = _f(m.get("net_pf", m.get("profit_factor", m.get("pf"))))
        n = _f(m.get("n", m.get("n_trades", m.get("n_signals"))))
        pnl = _f(m.get("total_pnl", m.get("net_pnl", m.get("pnl"))))
        wr = _f(m.get("winrate", m.get("win_rate")))
        mask = t.strategy_id == sid
        t.loc[mask, "bt_pf"] = pf
        t.loc[mask, "bt_n"] = n
        t.loc[mask, "bt_pnl"] = pnl
        t.loc[mask, "bt_wr"] = wr
        t.loc[mask, "row_state"] = st.get(int(sid), "?")

    print("=" * 100)
    print("BOOK-WIDE SHADOW LEDGER  2026-10-12 -> 2026-10-21  (SYNTHETIC, 1 contract, "
          "cost_model=v1_realistic_micros, adverse tie-break, one-trade-at-a-time)")
    print("=" * 100)
    n = len(t)
    print(f"shadow trades resolved : {n}   (WIN {(t.outcome=='WIN').sum()} / "
          f"LOSS {(t.outcome=='LOSS').sum()} / OPEN {(t.outcome=='OPEN').sum()})")
    print(f"shadow PF (all resolved): {pf_of(t):.3f}   shadow $ (1 contract): {t.pnl.sum():,.2f}   "
          f"avg R: {t.r_mult.mean():+.3f}")

    print("\n--- by symbol ---")
    g = t.groupby("symbol")
    print(f"{'sym':<5}{'n':>6}{'W':>6}{'L':>6}{'PF':>8}{'shadow$':>12}{'avgR':>8}{'risk$/tr':>10}")
    for s, x in g:
        print(f"{s:<5}{len(x):>6}{(x.outcome=='WIN').sum():>6}{(x.outcome=='LOSS').sum():>6}"
              f"{pf_of(x):>8.3f}{x.pnl.sum():>12,.2f}{x.r_mult.mean():>8.3f}{x.risk_dollars.mean():>10.2f}")

    print("\n--- by timeframe (of the strategy that produced the signal) ---")
    for s, x in t.groupby("tf"):
        print(f"{s:<5}{len(x):>6}{(x.outcome=='WIN').sum():>6}{(x.outcome=='LOSS').sum():>6}"
              f"{pf_of(x):>8.3f}{x.pnl.sum():>12,.2f}{x.r_mult.mean():>8.3f}"
              f"{x.risk_dollars.mean():>10.2f}")

    print("\n--- by session (ET: Asia <03:00, London 03:00-08:00, NY AM 08:00-12:00, NY PM 12:00-16:45) ---")
    for s, x in t.groupby("session"):
        print(f"{s:<8}{len(x):>6}{(x.outcome=='WIN').sum():>6}{(x.outcome=='LOSS').sum():>6}"
              f"{pf_of(x):>8.3f}{x.pnl.sum():>12,.2f}{x.r_mult.mean():>8.3f}"
              f"{x.risk_dollars.mean():>10.2f}")

    print("\n--- symbol x session matrix (avg R, n in parens) ---")
    piv = t.pivot_table(index="symbol", columns="session", values="r_mult", aggfunc="mean")
    cnt = t.pivot_table(index="symbol", columns="session", values="r_mult", aggfunc="size")
    for s in piv.index:
        cells = "  ".join(f"{c}={piv.loc[s,c]:+.3f}({int(cnt.loc[s,c])})" for c in piv.columns)
        print(f"{s:<5} {cells}")

    # ---------------------------------------------------- per strategy row
    rows = []
    for sid, x in t.groupby("strategy_id"):
        bt_pf = x.bt_pf.iloc[0]
        rows.append({
            "strategy_id": sid, "symbol": x.symbol.iloc[0], "tf": x.tf.iloc[0],
            "csid": x.csid.iloc[0], "family": str(x.family.iloc[0]),
            "strategy_name": str(x.strategy_name.iloc[0]),
            "slot_key": str(x.slot_key.iloc[0]), "state": str(x.row_state.iloc[0]),
            "n": len(x), "w": int((x.outcome == "WIN").sum()),
            "l": int((x.outcome == "LOSS").sum()), "open": int((x.outcome == "OPEN").sum()),
            "pf": pf_of(x), "shadow_$": x.pnl.sum(), "avgR": x.r_mult.mean(),
            "risk$": x.risk_dollars.mean(), "bt_pf": bt_pf, "bt_n": x.bt_n.iloc[0],
            "bt_pnl": x.bt_pnl.iloc[0],
        })
    S = pd.DataFrame(rows).sort_values("shadow_$", ascending=False)
    S.to_csv(REPO / "research" / "task2850_shadow_by_strategy.csv", index=False)
    print(f"\n--- per-strategy rows with >=20 shadow trades: "
          f"{int((S.n>=20).sum())} of {len(S)} ---")
    cols = ["strategy_id", "symbol", "tf", "csid", "family", "n", "w", "l", "pf",
            "shadow_$", "avgR", "bt_pf", "bt_n", "state"]
    show = S[S.n >= 20][cols].copy()
    show["pf"] = show.pf.round(3)
    show["avgR"] = show.avgR.round(3)
    show["shadow_$"] = show["shadow_$"].round(2)
    print(show.head(40).to_string(index=False))
    print(f"\nwrote {REPO/'research'/'task2850_shadow_by_strategy.csv'}")

    # ------------------------------------------- per architecture (dedupe)
    t["arch"] = t.symbol + "|" + t.csid.astype(str) + "|" + t.tf
    arows = []
    for a, x in t.groupby("arch"):
        arows.append({
            "arch": a, "symbol": x.symbol.iloc[0], "tf": x.tf.iloc[0], "csid": x.csid.iloc[0],
            "n_rows": x.strategy_id.nunique(), "n": len(x), "w": int((x.outcome == "WIN").sum()),
            "l": int((x.outcome == "LOSS").sum()), "pf": pf_of(x),
            "shadow_$": x.pnl.sum(), "avgR": x.r_mult.mean(),
            "fams": ";".join(sorted(set(x.family.astype(str))))[:70],
        })
    A = pd.DataFrame(arows).sort_values("shadow_$", ascending=False)
    A.to_csv(REPO / "research" / "task2850_shadow_by_architecture.csv", index=False)
    print(f"\n--- per-architecture (symbol|csid|tf), top 25 by shadow_$ ---")
    aa = A.head(25).copy()
    aa["pf"] = aa.pf.round(3); aa["avgR"] = aa.avgR.round(3); aa["shadow_$"] = aa["shadow_$"].round(2)
    print(aa[["arch", "n_rows", "n", "w", "l", "pf", "shadow_$", "avgR"]].to_string(index=False))

    # ---------------------------------------------- contradiction flagging
    ok = S.dropna(subset=["bt_pf"]).copy()
    contra = ok[(ok.bt_pf >= 1.3) & (ok.pf < 0.9) & (ok.n >= 20)]
    contra2 = ok[(ok.bt_pf < 1.0) & (ok.pf >= 1.3) & (ok.n >= 20)]
    print(f"\n--- CONTRADICTION FLAGS (n_shadow>=20) ---")
    print(f"BT PF>=1.3 but shadow PF<0.9 : {len(contra)} rows")
    print(f"BT PF<1.0 but shadow PF>=1.3 : {len(contra2)} rows")
    cc = contra[["strategy_id", "symbol", "tf", "csid", "family", "n", "pf", "shadow_$",
                 "bt_pf", "bt_n", "bt_pnl", "state"]].copy()
    cc["pf"] = cc.pf.round(3); cc["shadow_$"] = cc["shadow_$"].round(2)
    print(cc.sort_values("shadow_$").to_string(index=False))
    print()
    c2 = contra2[["strategy_id", "symbol", "tf", "csid", "family", "n", "pf", "shadow_$",
                  "bt_pf", "bt_n", "bt_pnl", "state"]].copy()
    c2["pf"] = c2.pf.round(3); c2["shadow_$"] = c2["shadow_$"].round(2)
    print(c2.sort_values("shadow_$", ascending=False).to_string(index=False))
    contra.to_csv(REPO / "research" / "task2850_contradictions.csv", index=False)

    # ---------------------------------------------------- zero-signal rows
    sig_ids = set(int(x) for x in t.strategy_id.unique())
    all_rows = pd.read_sql("SELECT id, symbol, strategy_family, state FROM active_strategies "
                           "WHERE state='active'", con) if False else None
    con2 = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    aa2 = pd.read_sql("SELECT id, symbol, strategy_family, params_json, source_candidate_id, "
                      "activated_at_utc FROM active_strategies WHERE state='active'", con2)
    con2.close()
    def tf_of(p):
        try:
            return norm(str(json.loads(p or "{}").get("timeframe")))
        except Exception:
            return "1h"
    aa2["tf"] = aa2.params_json.map(tf_of)
    aa2["csid"] = aa2.params_json.map(lambda p: str(json.loads(p or "{}").get("custom_strategy_id", "")))
    fired = set(aa2.csid) & set(t.csid.astype(str)) - {""}
    zero = aa2[~aa2.csid.astype(str).isin(fired)]
    print(f"\n--- ACTIVE ROWS WITH ZERO SHADOW SIGNALS: {len(zero)} of {len(aa2)} ---")
    zt = zero.groupby(["symbol", "tf"]).size().unstack(fill_value=0)
    print(zt.to_string())
    zero.to_csv(REPO / "research" / "task2850_zero_signal_actives.csv", index=False)

    # ------------------------------------------- new wave cross-check (D)
    print("\n--- (D) NEW-PROMOTED WAVE CROSS-CHECK ---")
    print(cand[["id", "symbol", "strategy_family", "status"]].to_string(index=False)
          if len(cand) else "candidates 7368-7372 not found")
    wave = aa2[aa2.source_candidate_id.isin([7368, 7369, 7370, 7371, 7372])]
    print("\nactive rows promoted from the wave:")
    print(wave[["id", "symbol", "tf", "csid", "strategy_family", "activated_at_utc"]].to_string(index=False))
    wave_csids = set(wave.csid.astype(str)) - {""}
    print(f"\nwave csids: {sorted(wave_csids)}")
    sub = t[t.csid.astype(str).isin(wave_csids)]
    print(f"shadow signals in window carrying a wave csid: {len(sub)}")
    if len(sub):
        print(sub.groupby(["symbol", "tf", "csid"]).outcome.value_counts().to_string())
    # sibling search: any signal in window from any row whose family contains
    # 'fvg' or 'hourly_bias'
    sib = t[t.family.astype(str).str.contains("fvg|hourly_bias", case=False, regex=True)]
    print(f"\nsibling search (family ~ fvg|hourly_bias) signals: {len(sib)}")
    if len(sib):
        print(sib.groupby(["symbol", "tf", "family"]).outcome.value_counts().to_string())


def norm(tf: str) -> str:
    return {"5m": "5m", "15m": "15m", "1h": "1h", "1H": "1h"}.get(tf, "1h")


if __name__ == "__main__":
    main()