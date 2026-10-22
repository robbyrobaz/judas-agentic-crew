"""ARCHIVE_RESTAMP_VERDICTS_2026-10-22 -- task #2861.

Re-measure the load-bearing BT envelopes on the REAL deep archive
(`src/bar_archive.py`) instead of the 13-27 day `bar_cache` window, and emit the
SPAN-INFLATION FACTOR (cache-window PF / archive PF) that the operator needs to
decide whether the 174-actives book can be trusted.

Why this is a script and not N tool calls
-----------------------------------------
`run_custom_backtest()` (the tool) re-fetches bars through `bar_cache` on every
single call, which (a) needs a live IBKR socket, (b) cannot be pointed at a
specific window, and (c) makes a 13-vs-120-day A/B impossible without two
different data sources. This calls the SAME production engine
(`run_custom_backtest_on_bars` -- identical cost model, identical aggregation,
identical `_bar_span_fields` stamp) with bars supplied explicitly, so the only
variable across the A/B is the WINDOW.

READ-ONLY: opens the DB with `sqlite3.connect(..., mode=ro)`. No writes.
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
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

DB = "judas_crew.db"

# The cache-equivalent window, per timeframe: `src/bar_cache._TF_SPECS` asks
# IBKR for 5m="10 D", 15m="20 D", 1h="60 D", and the measured realised hold was
# 2,730 bars at 5m (~13 days). We reproduce that ceiling honestly.
CACHE_EQ_BARS = {"5m": 2730, "15m": 20 * 96, "1h": 60 * 24}

# (row label, symbol, timeframe, custom_strategy_id, active_strategy_id or None)
TARGETS = [
    ("4835",  "MCL", "5m", 368, 4835),
    ("4874",  "MCL", "5m", 368, 4874),
    ("4634",  "MNQ", "1h", 229, 4634),
    ("4693",  "MNQ", "5m", 253, 4693),
    ("4794",  "MGC", "5m", 280, 4794),
    ("4995",  "MGC", "5m", 436, 4995),
    ("c7374", "MNQ", "5m", 437, None),
    ("c7375", "MGC", "15m", 438, None),
]

# Candidate rows carry their intended params on `strategy_candidates`, not on
# `active_strategies` (7374/7375 were promoted/rejected off the same envelope).
CAND_PARAMS = {7374: 7374, 7375: 7375}


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def intended_params(conn, active_id: int) -> dict:
    row = conn.execute(
        "SELECT params_json FROM active_strategies WHERE id=?", (active_id,)
    ).fetchone()
    if not row:
        return {}
    p = json.loads(row[0] or "{}")
    # Keep only what `evaluate(bars, params)` is allowed to see; runtime-only
    # keys (slot_key, architecture, *_utc, names) are not strategy params and
    # passing them through would let a strategy read its own slot metadata.
    drop = {"slot_key", "architecture", "strategy_name", "symbol",
            "strategy_family", "execution_engine", "custom_strategy_id",
            "cost_model", "session_start_utc", "session_end_utc"}
    return {k: v for k, v in p.items() if k not in drop}


def code_for(conn, csid: int) -> str:
    row = conn.execute(
        "SELECT code FROM custom_strategies WHERE id=?", (csid,)
    ).fetchone()
    return row[0] if row else ""


def registered_bt(conn, csid: int) -> dict:
    row = conn.execute(
        "SELECT backtest_metrics_json FROM custom_strategies WHERE id=?", (csid,)
    ).fetchone()
    try:
        return json.loads((row[0] if row else "") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def measure(code, bars, params, symbol, qty=1, timeout_s=900):
    t0 = time.time()
    res = run_custom_backtest_on_bars(
        code=code, bars=bars, params=params, symbol=symbol,
        qty=qty, timeout_s=timeout_s,
    )
    res["wall_s"] = round(time.time() - t0, 1)
    return res


def slim(res: dict) -> dict:
    n = int(res.get("n_trades", 0) or 0)
    w = int(res.get("n_wins", 0) or 0)
    return {
        "n": n,
        "W": w,
        "L": int(res.get("n_losses", 0) or 0),
        "pf": round(float(res.get("pf", 0.0) or 0.0), 3),
        "wr_pct": round(100.0 * w / n, 1) if n else 0.0,
        "e_r": round(float(res.get("expectancy_r", 0.0) or 0.0), 3),
        "total_usd": round(float(res.get("total_pnl", 0.0) or 0.0), 2),
        "max_dd_usd": round(float(res.get("max_drawdown", 0.0) or 0.0), 2),
        "span_days": res.get("bars_span_days"),
        "n_bars": res.get("n_bars"),
        "first_bar": res.get("first_bar"),
        "last_bar": res.get("last_bar"),
        "cost_model": res.get("cost_model", COST_MODEL_VERSION),
        "error": res.get("error"),
        "wall_s": res.get("wall_s"),
    }


def main():
    conn = ro_conn()
    out = {"cost_model_version": COST_MODEL_VERSION, "rows": []}

    for label, sym, tf, csid, act_id in TARGETS:
        rec = {"row": label, "symbol": sym, "timeframe": tf,
               "custom_strategy_id": csid, "active_strategy_id": act_id}

        bars_all = bar_archive.read_archive(sym, tf)
        if bars_all.empty:
            rec["archive_status"] = "MISSING"
            rec["not_restampable"] = True
            out["rows"].append(rec)
            print(f"[{label}] {sym} {tf}: ARCHIVE MISSING -- NOT-RESTAMPABLE", flush=True)
            continue

        span = (bars_all["ts"].iloc[-1] - bars_all["ts"].iloc[0]).days
        rec["archive_bars"] = int(len(bars_all))
        rec["archive_span_days"] = int(span)
        rec["archive_first"] = str(bars_all["ts"].iloc[0])
        rec["archive_last"] = str(bars_all["ts"].iloc[-1])
        if span < 60:
            rec["not_restampable"] = True
            rec["archive_status"] = f"THIN ({span}d < 60d)"

        if act_id is not None:
            params = intended_params(conn, act_id)
        elif csid in CAND_PARAMS:
            row = conn.execute(
                "SELECT params_json FROM strategy_candidates WHERE id=?",
                (CAND_PARAMS[csid],),
            ).fetchone()
            params = intended_params(conn, None) if not row else {}
            if row:
                p = json.loads(row[0] or "{}")
                drop = {"slot_key", "architecture", "strategy_name", "symbol",
                        "strategy_family", "execution_engine",
                        "custom_strategy_id", "cost_model"}
                params = {k: v for k, v in p.items() if k not in drop}
        else:
            params = {}
        code = code_for(conn, csid) if csid else ""
        rec["intended_params"] = params
        if not code:
            rec["archive_status"] = rec.get("archive_status", "") + " NO_CODE"
            out["rows"].append(rec)
            print(f"[{label}] {sym} {tf}: no code for csid={csid}", flush=True)
            continue

        rec["registered_bt_metrics"] = registered_bt(conn, csid)

        # ---- A: cache-equivalent window (what every promote actually used) ----
        cap = CACHE_EQ_BARS.get(tf, 2730)
        tail = bars_all.tail(cap).reset_index(drop=True)
        rec["cache_eq_bars_used"] = int(len(tail))
        rec["CACHE_EQ"] = slim(measure(code, tail, params, sym))
        print(f"[{label}] {sym} {tf} CACHE-EQ -> {rec['CACHE_EQ']}", flush=True)

        # ---- B: the real archive span ----
        rec["ARCHIVE"] = slim(measure(code, bars_all, params, sym))
        print(f"[{label}] {sym} {tf} ARCHIVE  -> {rec['ARCHIVE']}", flush=True)

        # ---- span inflation ----
        cpf, apf = rec["CACHE_EQ"]["pf"], rec["ARCHIVE"]["pf"]
        c_usd, a_usd = rec["CACHE_EQ"]["total_usd"], rec["ARCHIVE"]["total_usd"]
        rec["span_inflation_pf"] = round(cpf / apf, 3) if apf else None
        rec["span_inflation_usd"] = round(c_usd / a_usd, 3) if a_usd else None
        # joint BT-LURE (finding 6511): PF>3 AND WR>70% together
        rec["lure_cache"] = bool(cpf > 3 and rec["CACHE_EQ"]["wr_pct"] > 70)
        rec["lure_archive"] = bool(apf > 3 and rec["ARCHIVE"]["wr_pct"] > 70)

        out["rows"].append(rec)
        with open("outputs/restamp_20261022.json", "w") as fh:
            json.dump(out, fh, indent=2, default=str)

    with open("outputs/restamp_20261022.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\n=== SUMMARY ===")
    for r in out["rows"]:
        if "ARCHIVE" not in r:
            print(f"{r['row']:>6} {r['symbol']:>4} {r['timeframe']:>3}  "
                  f"{r.get('archive_status','?')}")
            continue
        print(f"{r['row']:>6} {r['symbol']:>4} {r['timeframe']:>3}  "
              f"cache {r['CACHE_EQ']['pf']:>6} / n={r['CACHE_EQ']['n']:<4} "
              f"-> archive {r['ARCHIVE']['pf']:>6} / n={r['ARCHIVE']['n']:<4} "
              f"span={r['ARCHIVE']['span_days']}d  infl_pf={r['span_inflation_pf']}")


if __name__ == "__main__":
    main()