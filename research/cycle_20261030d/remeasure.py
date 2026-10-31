"""Production-fill re-measurement of library custom strategies.

Runs src.research.custom_strategy_runtime.run_custom_backtest (the SAME
sanctioned entry point the agent tool uses) with fill_mode='production'
(entry decided on the close of bar T fills at the OPEN of bar T+1),
cost_model=v1_realistic_micros, qty=1, dollar denominated.

Usage: .venv/bin/python research/cycle_20261030d/remeasure.py cells.json
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath("."))
from src.research import custom_strategy_runtime as csr  # noqa: E402

DB = "judas_crew.db"
TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 0.0000005}


def load_code(csid):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT code, name FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    finally:
        conn.close()
    return row


def main():
    cells = json.load(open(sys.argv[1]))
    out = []
    for cell in cells:
        csid = cell.get("csid", cell.get("file"))
        sym = cell["symbol"]
        tf = cell.get("timeframe", "5m")
        days = int(cell.get("days", 90))
        if "file" in cell:
            code = open(cell["file"]).read()
            name = cell.get("name", os.path.basename(cell["file"]))
        else:
            row = load_code(csid)
            if row is None:
                print(f"{csid} MISSING"); continue
            code, name = row
        params = cell.get("params") or {}
        params = dict(params)
        params.setdefault("tick", TICK[sym])
        t0 = time.time()
        try:
            m = csr.run_custom_backtest(code=code, symbol=sym, days=days,
                                        timeframe=tf, params=params, qty=1,
                                        fill_mode="production", timeout_s=600)
        except Exception as exc:  # noqa: BLE001
            print(f"{csid} {sym} {tf} ERROR {type(exc).__name__}: {exc}")
            continue
        rec = {
            "csid": cell.get("csid", cell.get("file")), "name": name, "symbol": sym, "timeframe": tf,
            "days_req": days, "n_trades": m.get("n_trades"), "n_sig": m.get("n_signals"),
            "pf": round(float(m.get("pf", 0)), 3), "pnl": round(float(m.get("total_pnl", 0)), 2),
            "er": round(float(m.get("expectancy_r", 0)), 3),
            "dd": round(float(m.get("max_drawdown", 0)), 2),
            "wr": round(100.0 * m.get("n_wins", 0) / max(1, m.get("n_trades", 0)), 1),
            "span_d": m.get("bars_span_days"), "nbars": m.get("n_bars"),
            "grade": m.get("promotion_grade"), "stale": m.get("stale_fill_n"),
            "slip": m.get("entry_slip_ticks_abs_mean"),
            "cost": m.get("cost_model"), "err": m.get("error"),
            "secs": round(time.time() - t0, 1),
        }
        out.append(rec)
        print(json.dumps(rec), flush=True)
    with open(sys.argv[1].replace(".json", "_out.json"), "w") as fh:
        json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
