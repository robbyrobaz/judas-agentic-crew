"""FAIR_ENTRY_BOOK_REMEASURE population build -- task #2869.

READ-ONLY. Dumps the archive-covered active population with everything the
restamp needs: id, symbol, tf, csid, engine, params.  Also emits the (D)
marker screen inputs: live closed-trade $ / trade / WR per active.
"""
from __future__ import annotations

import json
import sqlite3

DB = "judas_crew.db"
OUT = "outputs/fair_book_pop_20261022.json"

ARCHIVE_CELLS = {("MGC", "5m"), ("MGC", "15m"), ("MNQ", "5m"),
                 ("MNQ", "15m"), ("MNQ", "1h"), ("MCL", "5m")}


def norm_tf(v):
    s = str(v or "").strip().lower().replace("-", "").replace(" ", "")
    return {"5m": "5m", "15m": "15m", "1h": "1h", "1h": "1h",
            "1hour": "1h", "60m": "1h", "30m": "30m", "": ""}.get(s, s)


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    acts = con.execute(
        "SELECT id, symbol, strategy_family, version, params_json, activated_at_utc "
        "FROM active_strategies WHERE state='active' ORDER BY symbol, id").fetchall()

    # live per-active closed trades
    live = {}
    for r in con.execute(
        "SELECT strategy_id, COUNT(*) n, SUM(CASE WHEN pnl_dollars>0 THEN 1 ELSE 0 END) w, "
        "SUM(COALESCE(pnl_dollars,0)) pnl, MIN(opened_at) f, MAX(opened_at) l "
        "FROM trades WHERE status='closed' AND strategy_id IS NOT NULL GROUP BY strategy_id"
    ):
        live[r["strategy_id"]] = dict(r)

    rows = []
    for a in acts:
        p = json.loads(a["params_json"] or "{}")
        tf = norm_tf(p.get("timeframe"))
        sym = a["symbol"].upper()
        csid = p.get("custom_strategy_id")
        rows.append({
            "id": a["id"], "symbol": sym, "family": a["strategy_family"],
            "version": a["version"], "tf": tf, "csid": csid,
            "engine": p.get("execution_engine"),
            "activated": a["activated_at_utc"],
            "covered": (sym, tf) in ARCHIVE_CELLS,
            "params": p,
            "live": live.get(a["id"]),
        })

    con.close()

    covered = [r for r in rows if r["covered"]]
    # distinct measurement units = (csid or family, symbol, tf, params-subset)
    units = {}
    for r in covered:
        key = (r["csid"], r["symbol"], r["tf"])
        units.setdefault(key, []).append(r["id"])

    out = {
        "n_actives": len(rows),
        "n_covered": len(covered),
        "n_units": len(units),
        "by_symbol_tf": {},
        "units": [{"csid": k[0], "symbol": k[1], "tf": k[2], "ids": v}
                  for k, v in sorted(units.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2]))],
        "rows": rows,
    }
    for r in rows:
        out["by_symbol_tf"].setdefault(f"{r['symbol']} {r['tf']}", 0)
        out["by_symbol_tf"][f"{r['symbol']} {r['tf']}"] += 1

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps({"n_actives": out["n_actives"], "n_covered": out["n_covered"],
                      "n_units": out["n_units"],
                      "by_symbol_tf": out["by_symbol_tf"]}, indent=1))
    print("units:")
    for u in out["units"]:
        print(f"  csid={u['csid']} {u['symbol']} {u['tf']} ids={u['ids']}")


if __name__ == "__main__":
    main()