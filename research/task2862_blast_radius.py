"""task #2862 (B) -- duplicate-fire blast radius + first-N-fills concentration.

READ-ONLY. Uses the same inputs as task2862_arbitration_sim.py.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict

import pandas as pd

DB = "judas_crew.db"
LEDGER = "research/task2860b_shadow_strict.parquet"
POINT_VALUE = {"MGC": 10.0, "MNQ": 2.0, "MCL": 100.0, "ZF": 1000.0, "6J": 12_500_000.0}
SIZED_SYMBOLS = {"MGC", "MNQ", "MCL"}
CONTRACT_CAP, MAX_OPEN_POSITIONS, MAX_QTY = 10, 4, 5


def arch(family: str) -> str:
    """Collapse the 175 distinct strategy_family strings into architecture buckets."""
    f = family.lower()
    if "silver_bullet" in f or "pdh_pdl" in f or "pdb_pdl" in f:
        return "silver_bullet_pdh_pdl"
    if "ifvg" in f:
        return "ifvg_midpoint_reversion"
    if "mitigation_block" in f or "asian_range_sweep" in f:
        return "mitigation_block_sweep"
    if "inside_bar_disp" in f:
        return "inside_bar_disp_breakout"
    if "turtle_soup" in f:
        return "turtle_soup_sweep"
    if "ob_midpoint" in f or "sweep_first_ob" in f:
        return "ob_midpoint_reversion"
    if "bsl_run_judas" in f:
        return "bsl_run_judas_cluster"
    if "judas_continuation" in f:
        return "judas_continuation"
    if "atr_disp" in f or "disp_continuation" in f:
        return "atr_disp_continuation"
    if "session_disp" in f:
        return "session_disp_fvg_pullback"
    if "propulsion" in f:
        return "propulsion_block"
    if "sniper" in f:
        return "sniper_chain"
    if "unicorn" in f:
        return "unicorn_fvg_breaker"
    if "sfp" in f:
        return "sfp_liquidity_grab"
    if "custom" in f:
        return "custom_csid"
    if "buffet" in f:
        return "buffet_zoo"
    return f


def main() -> None:
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, symbol, strategy_family, version, params_json FROM active_strategies "
        "WHERE state='active'"
    ).fetchall()
    con.close()
    actives = []
    for r in rows:
        p = json.loads(r[4] or "{}")
        tf = p.get("timeframe", "1h")
        actives.append(dict(id=int(r[0]), symbol=r[1], family=r[2], version=int(r[3]),
                            tf=tf, arch=arch(r[2]),
                            csid=p.get("custom_strategy_id")))
    order = sorted(actives, key=lambda a: (a["symbol"], a["family"], -a["version"]))
    rank = {a["id"]: i for i, a in enumerate(order)}

    # ---- (B)(1) actives per (symbol, architecture, timeframe) ----
    bucket = Counter((a["symbol"], a["arch"], a["tf"]) for a in actives)
    print("=== (B)(1) actives per (symbol, architecture, timeframe); flag >=3 ===")
    flagged = 0
    for (s, ar, tf), n in sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0])):
        if n >= 3:
            flagged += 1
            print(f"  FLAG n={n:2d}  {s:4s} {ar:32s} {tf}")
    print(f"  buckets with n>=3: {flagged} of {len(bucket)}   (buckets of 1: "
          f"{sum(1 for v in bucket.values() if v == 1)})")
    sym_arch = Counter((a["symbol"], a["arch"]) for a in actives)
    print("  top (symbol, architecture) counts:", sym_arch.most_common(12))

    # duplicate-fire risk: actives in the SAME (symbol, arch, tf) bucket that can fire on the SAME bar
    print("\n=== (B)(2) same-bucket simultaneous-fire capacity (actives in one bucket) ===")
    for (s, ar, tf), n in sorted(bucket.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {s:4s} {ar:32s} {tf:3s} actives={n}  -> up to {n} concurrent rows on one bar")

    # ---- (B)(3) first-N admitted fills per session ----
    led = pd.read_parquet(LEDGER)
    led["rank"] = led["strategy_id"].map(rank)
    led = led[led["rank"].notna()].copy()
    led["rank"] = led["rank"].astype(int)
    led["risk_usd"] = led["risk_pts"].abs() * led["symbol"].map(POINT_VALUE).fillna(0.0)

    def qty_of(sym, risk_usd, cushion=2000.0):
        if sym not in SIZED_SYMBOLS or risk_usd <= 0:
            return 1
        budget = min(250.0, max(40.0, cushion * 0.05))
        return max(1, min(MAX_QTY, max(1, int(budget // risk_usd))))

    rows_out = []
    for (sess, ts), g in led.groupby(["sess", "ts_utc"]):
        g = g.sort_values("rank")
        placed, open_rows, contracts = 0, 0, 0
        adm = []
        for r in g.itertuples():
            q = qty_of(r.symbol, float(r.risk_usd))
            eff = placed
            if eff + q > CONTRACT_CAP:
                continue
            if open_rows >= MAX_OPEN_POSITIONS:
                continue
            placed += 1; open_rows += 1; contracts += q
            adm.append((r.symbol, int(r.strategy_id), q, float(r.r), float(r.risk_usd) * q))
        rows_out.append(dict(sess=sess, ts=ts, n_sig=len(g), n_adm=len(adm),
                             contracts=contracts,
                             symbols=[a[0] for a in adm], qtys=[a[2] for a in adm],
                             dollars=[a[4] for a in adm], r=[a[3] for a in adm]))
    df = pd.DataFrame(rows_out)

    # simulate a session: open rows persist until a trade closes -> approximate by
    # allowing 4 open rows per session and consuming them in signal order.
    print("\n=== (B)(3) first 10 ADMITTED fills inside a session ===")
    pure, dist = 0, Counter()
    sessions = df["sess"].unique()
    for s in sessions:
        sub = df[df["sess"] == s].sort_values("ts")
        seq = []
        for r in sub.itertuples():
            for sym, sid, q, rr, dd in zip(r.symbols, r.qtys, r.r, r.dollars) if False else []:
                pass
            # rebuild with the stored per-bar lists
            seq.extend([(sym, q, dd) for sym, q, dd in zip(r.symbols, r.qtys, r.dollars)])
        if len(seq) < 10:
            continue
        first10 = seq[:10]
        syms = [f[0] for f in first10]
        dist[len(set(syms))] += 1
        if len(set(syms)) == 1:
            pure += 1
    print("  distinct symbols in the first 10 admitted fills -> session count:", dict(dist))
    print(f"  sessions where the first 10 fills are ALL ONE SYMBOL: {pure}")

    # dollar blast radius of the first 10 fills
    worst = []
    for s in sessions:
        sub = df[df["sess"] == s].sort_values("ts")
        seq = []
        for r in sub.itertuples():
            seq.extend(list(zip(r.symbols, r.qtys, r.dollars)))
        if len(seq) >= 10:
            worst.append((s, sum(f[2] for f in seq[:10]), sum(f[1] for f in seq[:10])))
    if worst:
        vals = sorted(w[1] for w in worst)
        print(f"  $ P&L of the first 10 admitted fills (shadow model): "
              f"min={vals[0]:.0f} med={vals[len(vals)//2]:.0f} max={vals[-1]:.0f}")
        cq = sorted(w[2] for w in worst)
        print(f"  contracts used by the first 10 admitted fills: min={cq[0]} med={cq[len(cq)//2]} max={cq[-1]}")

    # ---- contracts per bar vs the cap ----
    print("\n=== contracts actually admitted on the busiest bars vs cap=10 ===")
    print(df.nlargest(8, "contracts")[["sess", "ts", "n_sig", "n_adm", "contracts"]].to_string(index=False))
    print("max contracts on a bar:", int(df["contracts"].max()),
          "| bars where contracts>10:", int((df["contracts"] > 10).sum()),
          "of", len(df))

    out = {
        "buckets_ge3": {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in bucket.items() if v >= 3},
        "n_buckets": len(bucket),
        "first10_symbol_mix": dict(dist),
        "first10_all_one_symbol_sessions": pure,
        "max_contracts_on_bar": int(df["contracts"].max()),
        "bars_over_cap10": int((df["contracts"] > 10).sum()),
        "bars": len(df),
    }
    with open("outputs/task2862_blast_radius.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nwrote outputs/task2862_blast_radius.json")


if __name__ == "__main__":
    main()
