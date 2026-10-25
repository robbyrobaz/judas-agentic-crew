"""task #2862 (A)+(B) -- READ-ONLY simulation of the runtime's cap arbitration.

Answers:
  * What order does run_portfolio_scan() iterate the actives in?  (derived from
    src/strategy_registry.py:951  ORDER BY symbol, strategy_family, version DESC)
  * If the 10-contract aggregate cap binds, WHICH strategies get it?
  * How concentrated are concurrent fires per (symbol, bar)?

Inputs (read-only):
  * judas_crew.db : active_strategies (state='active')
  * research/task2860b_shadow_strict.parquet : 9,296 forward signals from the
    #2860 shadow ledger (the only forward-signal record we have).

NO order placement, NO DB write, NO broker call.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict

import pandas as pd

DB = "judas_crew.db"
LEDGER = "research/task2860b_shadow_strict.parquet"

# src/portfolio_runtime.py _CONTRACT_SPECS (tick_value / tick == $ per price point)
POINT_VALUE = {"MGC": 10.0, "MNQ": 2.0, "MCL": 100.0, "ZF": 1000.0, "6J": 12_500_000.0}
# src/portfolio_runtime.py _EVAL_RISK_SYMBOLS -- only these get cushion-scaled sizing
SIZED_SYMBOLS = {"MGC", "MNQ", "MCL"}
MAX_QTY = 5           # _EVAL_MAX_QTY
CONTRACT_CAP = 10     # lucid_guard.RULES['max_contracts_aggregate']
MAX_OPEN_POSITIONS = 4   # config.yaml risk.max_open_positions
RISK_PCT, RISK_MIN, RISK_MAX = 0.05, 40.0, 250.0


def eval_sized_qty(symbol: str, qty: int, risk_dollars: float, cushion: float) -> int:
    """Mirror of src/portfolio_runtime.py:_eval_sized_qty (post-reset cushion)."""
    if symbol not in SIZED_SYMBOLS:
        return max(1, int(qty or 1))
    if risk_dollars <= 0:
        return max(1, int(qty or 1))
    budget = min(RISK_MAX, max(RISK_MIN, cushion * RISK_PCT))
    sized = int(budget // risk_dollars)
    return max(1, min(MAX_QTY, max(int(qty or 1), sized)))


def runtime_order() -> list[tuple]:
    """Exact iteration order of run_portfolio_scan(): list_active_strategies()."""
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, symbol, strategy_family, version FROM active_strategies "
        "WHERE state='active' ORDER BY symbol, strategy_family, version DESC"
    ).fetchall()
    con.close()
    return rows


def main() -> None:
    actives = runtime_order()
    rank = {int(a[0]): i for i, a in enumerate(actives)}
    print(f"actives={len(actives)}  first 8 in runtime order:")
    for a in actives[:8]:
        print("   ", a)

    led = pd.read_parquet(LEDGER)
    led["rank"] = led["strategy_id"].map(rank)
    missing = led[led["rank"].isna()]
    if len(missing):
        print(f"WARNING: {len(missing)} ledger signals belong to non-active strategies")
    led = led[led["rank"].notna()].copy()
    led["rank"] = led["rank"].astype(int)
    # risk in dollars for the sizing rule
    led["risk_usd"] = led["risk_pts"].abs() * led["symbol"].map(POINT_VALUE).fillna(0.0)

    groups = []
    for ts, g in led.groupby("ts_utc"):
        g = g.sort_values("rank")           # runtime iteration order within the bar
        groups.append((ts, g))

    print(f"\nbar-groups (distinct signal timestamps) = {len(groups)}")
    sizes = Counter(len(g) for _, g in groups)
    print("signals-per-bar distribution:", sorted(sizes.items())[:12], "... max:", max(sizes))

    # ---- simulate the admission arithmetic for a FRESH (post-reset) account ----
    # cushion after reset = 2000 (peak equity - 2000 MLL trail), so risk_budget=100.
    CUSHION = 2000.0
    admitted_rows, blocked_rows = [], []
    over_cap_rows = 0
    contracts_max = 0
    first10_symbol_pure = 0
    bar_with_10 = 0
    per_bar_detail = []
    for ts, g in groups:
        placed_trades = 0
        open_rows = 0
        contracts = 0
        admitted_syms = []
        for r in g.itertuples():
            qty = eval_sized_qty(r.symbol, 1, float(r.risk_usd), CUSHION)
            # _gate_fire: lucid cap uses (live NT contracts + trades placed THIS scan)
            eff = 0 + placed_trades
            if eff + qty > CONTRACT_CAP:
                blocked_rows.append((ts, r.symbol, int(r.strategy_id), qty, "lucid_contract_cap"))
                over_cap_rows += 1
                continue
            if open_rows >= MAX_OPEN_POSITIONS:
                blocked_rows.append((ts, r.symbol, int(r.strategy_id), qty, "max_open_positions"))
                continue
            placed_trades += 1
            open_rows += 1
            contracts += qty
            admitted_syms.append((r.symbol, int(r.strategy_id), qty))
            admitted_rows.append((ts, r.symbol, int(r.strategy_id), qty, float(r.r)))
        contracts_max = max(contracts_max, contracts)
        if len(admitted_syms) >= 10:
            bar_with_10 += 1
            syms = [a[0] for a in admitted_syms[:10]]
            if len(set(syms)) == 1:
                first10_symbol_pure += 1
        per_bar_detail.append({
            "ts": ts, "n_signals": int(len(g)), "n_admitted": len(admitted_syms),
            "contracts": contracts, "symbols": [a[0] for a in admitted_syms],
            "qty": [a[2] for a in admitted_syms],
        })

    adm = pd.DataFrame(admitted_rows, columns=["ts", "symbol", "strategy_id", "qty", "r"])
    print(f"\nadmitted={len(adm)}  blocked={len(blocked_rows)} "
          f"(cap-blocked={over_cap_rows}, open-pos-blocked={len(blocked_rows)-over_cap_rows})")
    print("max contracts on one bar (cap says 10):", contracts_max)
    print("admitted fills by symbol:", adm["symbol"].value_counts().to_dict())
    print("qty distribution of admitted fills:", Counter(adm["qty"]).most_common())
    print(f"bars with >=10 admitted fills: {bar_with_10}; of those ALL-ONE-SYMBOL: {first10_symbol_pure}")
    if bar_with_10:
        sub = [d for d in per_bar_detail if d["n_admitted"] >= 10]
        pure = sum(1 for d in sub if len(set(d["symbols"][:10])) == 1)
        share = Counter()
        for d in sub:
            share.update(d["symbols"][:10])
        print("  first-10 symbol mix on those bars:", share.most_common())
        # how many contracts the first 10 fills represent
        c10 = [sum(d["qty"][:10]) for d in sub]
        print(f"  contracts consumed by the first 10 fills: min={min(c10)} med={sorted(c10)[len(c10)//2]} max={max(c10)}")

    # ---- duplicate-fire clustering: actives firing on the SAME bar, same symbol ----
    clust = Counter()
    fam_clust = Counter()
    for ts, g in groups:
        bysym = Counter(g["symbol"])
        for s, n in bysym.items():
            clust[(s, n)] += 1
        for (s, f), n in Counter(zip(g["symbol"], g["strategy_family"])).items():
            fam_clust[(s, f, n)] += 1
    print("\n(symbol, #simultaneous fires) -> #bars:", sorted(clust.items())[:20])
    top = sorted(fam_clust.items(), key=lambda kv: -kv[1])[:15]
    print("top (symbol, family, n_fires) clusters:", top)

    out = {
        "n_actives": len(actives),
        "bar_groups": len(groups),
        "admitted": int(len(adm)),
        "blocked": len(blocked_rows),
        "cap_blocked": over_cap_rows,
        "max_contracts_one_bar": contracts_max,
        "admitted_by_symbol": adm["symbol"].value_counts().to_dict(),
        "qty_dist": dict(Counter(adm["qty"]).most_common()),
        "bars_ge10_admitted": bar_with_10,
        "bars_ge10_all_one_symbol": first10_symbol_pure,
    }
    with open("outputs/task2862_arbitration_sim.json", "w") as f:
        json.dump(out, f, indent=2)
    print("\nwrote outputs/task2862_arbitration_sim.json")


if __name__ == "__main__":
    main()
