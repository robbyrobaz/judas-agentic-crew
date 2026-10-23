"""FAIR_ENTRY_BOOK_REMEASURE population build -- task #2869.

READ-ONLY w.r.t. registry/account. Writes only outputs/fair_book_pop_20261022.json.

Fixes carried in from the 2026-10-23 rebuild:

* ``ARCHIVE_CELLS`` was never defined and ``bar_archive`` was never imported --
  the builder crashed with NameError, so the population could not be rebuilt
  after new archives appeared.  Coverage is now DERIVED FROM DISK: a
  (symbol, tf) cell is covered iff ``bar_archive.read_archive`` returns a
  non-empty frame.  A newly built archive therefore becomes measurable
  automatically instead of staying "NOT-RESTAMPABLE" forever.
* ``custom_strategy_id`` is stored as a STRING in some ``params_json`` rows and
  as an int in others.  The restamp worker looks codes up in an int-keyed
  dict, so every string-csid unit silently fell through to "NO_CODE".  The
  csid is normalised to int here (and defensively re-normalised in the
  worker).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import bar_archive  # noqa: E402

DB = "judas_crew.db"
OUT = "outputs/fair_book_pop_20261022.json"

LEGAL = ("MGC", "MNQ", "MCL", "ZF", "6J")
TFS = ("5m", "15m", "1h")


def archive_cells():
    """(symbol, tf) pairs with a readable, non-empty archive on disk."""
    cells = set()
    for sym in LEGAL:
        for tf in TFS:
            try:
                df = bar_archive.read_archive(sym, tf)
            except Exception:  # noqa: BLE001
                df = None
            if df is not None and len(df) > 0:
                cells.add((sym, tf))
    return cells


def norm_tf(v):
    s = str(v or "").strip().lower().replace("-", "").replace(" ", "")
    return {"5m": "5m", "15m": "15m", "1h": "1h", "1hour": "1h", "60m": "1h",
            "30m": "30m", "": ""}.get(s, s)


def norm_csid(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    cells = archive_cells()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    acts = con.execute(
        "SELECT id, symbol, strategy_family, version, params_json, activated_at_utc "
        "FROM active_strategies WHERE state='active' ORDER BY symbol, id").fetchall()

    live = {}
    for r in con.execute(
        "SELECT strategy_id, COUNT(*) n, SUM(CASE WHEN pnl_dollars>0 THEN 1 ELSE 0 END) w, "
        "SUM(COALESCE(pnl_dollars,0)) pnl, MIN(opened_at) f, MAX(opened_at) l "
        "FROM trades WHERE status='closed' AND strategy_id IS NOT NULL GROUP BY strategy_id"
    ):
        live[r["strategy_id"]] = dict(r)
    con.close()

    rows = []
    for a in acts:
        p = json.loads(a["params_json"] or "{}")
        tf = norm_tf(p.get("timeframe"))
        sym = a["symbol"].upper()
        csid = norm_csid(p.get("custom_strategy_id"))
        rows.append({
            "id": a["id"], "symbol": sym, "family": a["strategy_family"],
            "version": a["version"], "tf": tf, "csid": csid,
            "engine": p.get("execution_engine"),
            "activated": a["activated_at_utc"],
            "covered": (sym, tf) in cells,
            "params": p,
            "live": live.get(a["id"]),
        })

    covered = [r for r in rows if r["covered"]]
    units = {}
    for r in covered:
        key = (r["csid"], r["symbol"], r["tf"])
        units.setdefault(key, []).append(r["id"])

    out = {
        "archive_cells": sorted([f"{s} {t}" for s, t in cells]),
        "n_actives": len(rows),
        "n_covered": len(covered),
        "n_units": len(units),
        "by_symbol_tf": {},
        "units": [{"csid": k[0], "symbol": k[1], "tf": k[2], "ids": v}
                  for k, v in sorted(units.items(),
                                     key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2]))],
        "rows": rows,
    }
    for r in rows:
        k = f"{r['symbol']} {r['tf']}"
        d = out["by_symbol_tf"].setdefault(k, {"n": 0, "covered": 0})
        d["n"] += 1
        if r["covered"]:
            d["covered"] += 1

    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(json.dumps({"archive_cells": out["archive_cells"],
                      "n_actives": out["n_actives"], "n_covered": out["n_covered"],
                      "n_units": out["n_units"],
                      "by_symbol_tf": out["by_symbol_tf"]}, indent=1))


if __name__ == "__main__":
    main()
