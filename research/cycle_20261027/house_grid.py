"""HOUSE-ENGINE GRID (2026-10-27 cycle) -- parallel v1 stamps over a grid.

WHY THIS FILE
-------------
`run_custom_backtest` (the tool) is ONE cell at a time and each cell burns a
tool call.  The house engine behind it is `csr.run_custom_backtest`, which
carries the SAME fill model (fill at the named price), the SAME cost model
(cost_model=v1_realistic_micros) and the SAME 60s wall-clock cap.  This file
calls that same function over a grid in parallel so a researcher can screen
100 cells in the time one tool call would screen 5, and then re-run the
SURVIVOR through the tool itself so the promoted stamp is the authoritative
one.

Usage:
  .venv/bin/python research/cycle_20261027/house_grid.py <template.py> <symbol> <tf> <days> <json>
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

MARK = "# --- CELL ---"


def _one(job):
    from src.research import custom_strategy_runtime as csr
    tpl, cell, symbol, tf, days = job
    code = tpl.replace(MARK, "CELL = " + repr(cell))
    m = csr.run_custom_backtest(code=code, symbol=symbol, days=days,
                                 timeframe=tf, params=None, qty=1)
    return cell, m


def main():
    tpl_path = Path(sys.argv[1])
    symbol = sys.argv[2]
    tf = sys.argv[3]
    days = int(sys.argv[4])
    out_path = Path(sys.argv[5])
    cells = json.loads(tpl_path.with_suffix(".cells.json").read_text())
    tpl = tpl_path.read_text()
    jobs = [(tpl, c, symbol, tf, days) for c in cells]
    rows = []
    with ProcessPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        for cell, m in ex.map(_one, jobs):
            rows.append({"cell": cell, "metrics": m})
            print(f"{json.dumps(cell, sort_keys=True)}\n    pf={m.get('pf')} n={m.get('n_trades')} "
                  f"pnl={m.get('total_pnl')} e_r={m.get('expectancy_r')} err={m.get('error')} "
                  f"stale={m.get('stale_fill_n')}", flush=True)
    out_path.write_text(json.dumps(rows, indent=2, default=str))
    print("wrote", out_path)


if __name__ == "__main__":
    main()
