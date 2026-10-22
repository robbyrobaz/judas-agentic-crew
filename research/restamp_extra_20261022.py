"""Extra restamp rows for #2861 section (D): the two rows whose ffea9e3c
verdicts the operator explicitly asked us to rule on.

  #4905  MNQ 5m  ob_midpoint_reversion_htf_bias csid384 -- "REJECT reinstatement"
  #4990  MNQ 15m custom / hourly-bias FVG       csid434 -- "CONFIRM REINSTATE"

Same production engine, same cost model, cache-equivalent window vs deep archive.
READ-ONLY.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bar_archive  # noqa: E402
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars  # noqa: E402

DB = "judas_crew.db"
CACHE_EQ_BARS = {"5m": 2730, "15m": 20 * 96, "1h": 60 * 24}
TARGETS = [
    ("4905", "MNQ", "5m", 384, 4905),
    ("4990", "MNQ", "15m", 434, 4990),
]
DROP = {"slot_key", "architecture", "strategy_name", "symbol", "strategy_family",
        "execution_engine", "custom_strategy_id", "cost_model",
        "session_start_utc", "session_end_utc"}


def run(code, bars, params, sym):
    r = run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                    symbol=sym, qty=1, timeout_s=900)
    n = int(r.get("n_trades", 0) or 0)
    w = int(r.get("n_wins", 0) or 0)
    return {"n": n, "W": w, "L": int(r.get("n_losses", 0) or 0),
            "pf": round(float(r.get("pf", 0) or 0), 3),
            "wr_pct": round(100.0 * w / n, 1) if n else 0.0,
            "e_r": round(float(r.get("expectancy_r", 0) or 0), 3),
            "total_usd": round(float(r.get("total_pnl", 0) or 0), 2),
            "max_dd_usd": round(float(r.get("max_drawdown", 0) or 0), 2),
            "span_days": r.get("bars_span_days"), "n_bars": r.get("n_bars"),
            "cost_model": r.get("cost_model"), "error": r.get("error")}


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = {"rows": []}
    for label, sym, tf, csid, act_id in TARGETS:
        code = conn.execute("SELECT code FROM custom_strategies WHERE id=?",
                            (csid,)).fetchone()
        p = json.loads(conn.execute(
            "SELECT params_json FROM active_strategies WHERE id=?",
            (act_id,)).fetchone()[0] or "{}")
        params = {k: v for k, v in p.items() if k not in DROP}
        bars = bar_archive.read_archive(sym, tf)
        rec = {"row": label, "symbol": sym, "tf": tf, "csid": csid,
               "registered_metrics": json.loads(conn.execute(
                   "SELECT metrics_json FROM active_strategies WHERE id=?",
                   (act_id,)).fetchone()[0] or "{}"),
               "intended_params": params, "archive_bars": int(len(bars))}
        tail = bars.tail(CACHE_EQ_BARS[tf]).reset_index(drop=True)
        rec["CACHE_EQ"] = run(code, tail, params, sym)
        print(f"[{label}] CACHE-EQ -> {rec['CACHE_EQ']}", flush=True)
        t0 = time.time()
        rec["ARCHIVE"] = run(code, bars.reset_index(drop=True), params, sym)
        rec["ARCHIVE"]["wall_s"] = round(time.time() - t0, 1)
        print(f"[{label}] ARCHIVE  -> {rec['ARCHIVE']}", flush=True)
        c, a = rec["CACHE_EQ"], rec["ARCHIVE"]
        rec["span_inflation_pf"] = round(c["pf"] / a["pf"], 3) if a["pf"] else None
        cpt = c["total_usd"] / c["n"] if c["n"] else None
        apt = a["total_usd"] / a["n"] if a["n"] else None
        rec["usd_per_trade_cache"] = round(cpt, 2) if cpt else None
        rec["usd_per_trade_archive"] = round(apt, 2) if apt else None
        rec["span_inflation_usd_per_trade"] = round(cpt / apt, 3) if cpt and apt else None
        rec["lure_archive"] = bool(a["pf"] > 3 and a["wr_pct"] > 70)
        out["rows"].append(rec)
        with open("outputs/restamp_extra_20261022.json", "w") as fh:
            json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()