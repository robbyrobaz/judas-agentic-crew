"""Dump custom_strategies code + prior metrics for the LIBRARY members.

Read-only against judas_crew.db.  Writes the strategy source into
research/cycle_20261029c/codes/ so the honest screen can compile it exactly
as the house engine would.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "research" / "cycle_20261029c" / "codes"
OUT.mkdir(parents=True, exist_ok=True)

IDS = [int(x) for x in sys.argv[1:]] or [270, 403, 409, 237, 348, 401, 368, 400,
                                           357, 253, 295, 332, 354, 194, 229,
                                           333, 203, 262, 424]

con = sqlite3.connect(f"file:{ROOT/'judas_crew.db'}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
for i in IDS:
    row = con.execute("SELECT id, name, symbol, code, rationale, "
                       "backtest_metrics_json, active FROM custom_strategies "
                       "WHERE id=?", (i,)).fetchone()
    if row is None:
        print(f"csid {i}: NOT FOUND")
        continue
    (OUT / f"csid{i}.py").write_text(row["code"])
    try:
        bm = json.loads(row["backtest_metrics_json"])
    except Exception:
        bm = {"_raw": str(row["backtest_metrics_json"])[:400]}
    print(f"csid {i}: {row['name']}  sym={row['symbol']} active={row['active']}")
    print(f"   metrics: {json.dumps(bm)[:600]}")
    # pull the CELL defaults out of the source so the screen uses the same params
    src = row["code"]
    i0 = src.find("CELL")
    if i0 >= 0:
        print("   CELL:", src[i0:i0 + 700].replace("\n", " ")[:700])
con.close()
