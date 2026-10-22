"""Split-half stability on the deep archive -- companion to #2861.

WHY
---
The #2861 restamp showed the book`s envelopes SURVIVE the 120-day window
(MCL 5m csid368 PF 6.33 n=1221; MNQ 1h csid229 PF 5.06 n=163; MNQ 5m csid253
PF 2.33 n=2439).  "Survives a wider window" is necessary but NOT sufficient:
a single 120-day stretch can still be one lucky regime.  The standard next
question is whether the edge is STABLE ACROSS SUB-PERIODS.

The 80-85% win rates on the silver-bullet rows are also far outside the band
finding 6511 says a genuine 2R edge occupies (PF 1.2-2.5 / WR 45-60%), and they
sit in direct contradiction with our own broker-confirmed book (MNQ 36.8% WR,
MGC 40.4%, ZF 36.8% -- 68 closed trades each, SIM mandate).  That contradiction
is a RESULT, not a footnote: a backtest that claims 83% WR on the same family
that loses 63% of its live trades is measuring something the live path cannot
reproduce.  Split-half + a cost/risk sanity probe is the cheapest way to say
where the optimism lives.

READ-ONLY: sqlite opened with mode=ro. No registry or account writes.
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

# (label, symbol, timeframe, csid, active_id)
TARGETS = [
    ("4835/4874", "MCL", "5m", 368, 4874),
    ("4634",      "MNQ", "1h", 229, 4634),
    ("4693",      "MNQ", "5m", 253, 4693),
]

DROP = {"slot_key", "architecture", "strategy_name", "symbol", "strategy_family",
        "execution_engine", "custom_strategy_id", "cost_model",
        "session_start_utc", "session_end_utc"}


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def load(conn, csid, act_id):
    code = conn.execute(
        "SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()[0]
    p = json.loads(conn.execute(
        "SELECT params_json FROM active_strategies WHERE id=?",
        (act_id,)).fetchone()[0] or "{}")
    return code, {k: v for k, v in p.items() if k not in DROP}


def run(code, bars, params, sym):
    r = run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                    symbol=sym, qty=1, timeout_s=900)
    n = int(r.get("n_trades", 0) or 0)
    w = int(r.get("n_wins", 0) or 0)
    return {
        "n": n, "W": w, "L": int(r.get("n_losses", 0) or 0),
        "pf": round(float(r.get("pf", 0) or 0), 3),
        "wr": round(100.0 * w / n, 1) if n else 0.0,
        "e_r": round(float(r.get("expectancy_r", 0) or 0), 3),
        "usd": round(float(r.get("total_pnl", 0) or 0), 2),
        "dd": round(float(r.get("max_drawdown", 0) or 0), 2),
        "span_d": r.get("bars_span_days"), "n_bars": r.get("n_bars"),
        "cost_model": r.get("cost_model"), "error": r.get("error"),
    }


def main():
    conn = ro()
    out = {"cost_model": "v1_realistic_micros", "rows": []}
    for label, sym, tf, csid, act_id in TARGETS:
        code, params = load(conn, csid, act_id)
        bars = bar_archive.read_archive(sym, tf)
        rec = {"row": label, "symbol": sym, "tf": tf, "csid": csid,
               "total_bars": int(len(bars))}
        # Four chronological quarters -- more informative than 2 halves because
        # a single bad quarter is visible instead of being averaged away.
        q = len(bars) // 4
        rec["quarters"] = []
        for k in range(4):
            chunk = bars.iloc[k * q:(k + 1) * q if k < 3 else len(bars)]
            chunk = chunk.reset_index(drop=True)
            t0 = time.time()
            res = run(code, chunk, params, sym)
            res["quarter"] = k + 1
            res["wall_s"] = round(time.time() - t0, 1)
            rec["quarters"].append(res)
            print(f"[{label}] Q{k+1} -> {res}", flush=True)
        pfs = [c["pf"] for c in rec["quarters"] if c["n"] >= 20]
        usds = [c["usd"] for c in rec["quarters"] if c["n"] >= 20]
        rec["profitable_quarters_of_rated"] = (
            f"{sum(1 for x in usds if x > 0)}/{len(usds)}")
        rec["pf_range_rated"] = [min(pfs), max(pfs)] if pfs else None
        out["rows"].append(rec)
        with open("outputs/splithalf_20261022.json", "w") as fh:
            json.dump(out, fh, indent=2, default=str)
    print("\n=== SUMMARY ===")
    for r in out["rows"]:
        print(f"{r['row']:>10} {r['symbol']:>4} {r['tf']:>3}  "
              f"profitable quarters {r['profitable_quarters_of_rated']}  "
              f"PF range {r['pf_range_rated']}")


if __name__ == "__main__":
    main()