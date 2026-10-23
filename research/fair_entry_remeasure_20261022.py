"""FAIR_ENTRY_REMEASURE_2026-10-22 -- re-measure custom envelopes under the fill
model the LIVE runtime actually uses.

Why this exists
---------------
Finding 07f3a4c2 proved `run_custom_backtest` fills at a price from a bar that
had already closed. That is defect #1. Reading `src/broker/ninjatrader.py`
proves a second, independent divergence that the retro-fill guard alone does
NOT fix:

    LIVE (src/broker/ninjatrader.py:886, :911-928)
        1. PLACE entry MARKET   <-- never at the strategy's `entry` price
        2. poll for the actual fill
        3. re-anchor stop/target DISTANCES to the ACTUAL fill price

    BACKTEST (src/research/custom_strategy_runtime.py:490-496)
        open_trade = {"entry": sig["entry"], ...}
        i.e. we are ASSUMED the strategy's requested price.

So even a strategy with `stale_fill_pct == 0` is being credited with a fill
model the live book never gets: a perfect fill at the author's chosen price.

This script re-measures an envelope under the live fill model:

    fair entry = OPEN of bar i+1      (the first bar after the decision bar --
                                      what NT's market order actually crosses)
    fair stop  = fair_entry -/+ |sig.entry - sig.stop|
    fair tgt   = fair_entry +/- |sig.target - sig.entry|
    gap-out    = if bar i+1's open is already beyond the stop, we are stopped
                 at that open (this is what really happens)

It ALSO reports the cheapest possible diagnostic, which needs no simulation:
how far the strategy's REQUESTED price was from the market at the moment it
asked (`req_gap_pts`). If |req_gap| is large, the fill model is fiction.

READ-ONLY: sqlite opened mode=ro, bars read from the archive.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import bar_archive  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade  # noqa: E402

DB = "judas_crew.db"
LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def code_for(conn, csid):
    r = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    if not r or not r[0]:
        return None
    return r[0].decode("utf-8", "replace") if isinstance(r[0], (bytes, bytearray)) else r[0]


def params_for(conn, aid):
    r = conn.execute("SELECT params_json FROM active_strategies WHERE id=?", (aid,)).fetchone()
    try:
        p = json.loads((r[0] if r else "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        p = {}
    drop = {"slot_key", "architecture", "strategy_name", "symbol",
            "strategy_family", "execution_engine", "custom_strategy_id",
            "cost_model", "session_start_utc", "session_end_utc"}
    return {k: v for k, v in p.items() if k not in drop}


def replay(code, bars, params, symbol, mode, timeout_s=900):
    """mode='signal_price' == the current engine.  mode='market_open' == live."""
    fn, meta = _compile_and_load(code)
    if fn is None:
        return {"error": meta.get("error")}
    from src.portfolio_runtime import _CONTRACT_SPECS
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    qty = int(params.get("qty", 1) or 1)

    pnls, rs, gaps = [], [], []
    open_t = None
    n_sig = 0
    n_stale = 0
    n_gapout = 0
    t0 = time.time()
    n = len(bars)
    for i in range(2, n):
        if time.time() - t0 > timeout_s:
            break
        if open_t is not None:
            bar = bars.iloc[i]
            hit_stop = ((open_t["d"] == "long" and float(bar["low"]) <= open_t["stop"]) or
                        (open_t["d"] == "short" and float(bar["high"]) >= open_t["stop"]))
            hit_target = ((open_t["d"] == "long" and float(bar["high"]) >= open_t["target"]) or
                          (open_t["d"] == "short" and float(bar["low"]) <= open_t["target"]))
            if hit_stop or hit_target:
                entry = open_t["entry"]
                risk = abs(entry - open_t["stop"]) or 1.0
                won = hit_target and not hit_stop
                pts = abs(open_t["target"] - entry) if won else -abs(entry - open_t["stop"])
                gross = pts * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason="target" if won else "stop", qty=qty)
                pnls.append(float(pnl))
                rd = risk * dpp * qty
                rs.append(float(pnl / rd) if rd else 0.0)
                open_t = None
            continue

        try:
            sig = fn(bars.iloc[: i + 1], dict(params))
        except Exception:
            continue
        if not isinstance(sig, dict):
            continue
        d = str(sig.get("direction", "")).lower()
        if d not in ("long", "short"):
            continue
        try:
            req = float(sig["entry"]); st = float(sig["stop"]); tg = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        n_sig += 1
        # requested price vs the market at the decision bar (signed: + == we
        # asked ABOVE the market, i.e. we would have paid up)
        mkt = float(bars.iloc[i]["close"])
        gaps.append((req - mkt) if d == "long" else (mkt - req))
        if sig.get("ts") is not None:
            try:
                if (pd.Timestamp(bars.iloc[i]["ts"]) - pd.Timestamp(sig["ts"])).total_seconds() > 90:
                    n_stale += 1
            except Exception:
                pass

        if mode == "signal_price":
            open_t = {"d": d, "entry": req, "stop": st, "target": tg}
            continue

        # ---- LIVE fill model: market order crosses at the NEXT bar's open ----
        if i + 1 >= n:
            continue
        nxt = bars.iloc[i + 1]
        fill = float(nxt["open"])
        stop_d = abs(req - st)
        tgt_d = abs(tg - req)
        if d == "long":
            stop = fill - stop_d; target = fill + tgt_d
        else:
            stop = fill + stop_d; target = fill - tgt_d
        # the order fills at `fill`; if the market already sits past our stop the
        # bracket dies on arrival (this is the 2026-07-20 live incident).
        if (d == "long" and fill <= stop) or (d == "short" and fill >= stop):
            n_gapout += 1
            gross = -stop_d * dpp * qty
            pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                       exit_reason="stop", qty=qty)
            pnls.append(float(pnl))
            rd = (stop_d * dpp * qty) or 1.0
            rs.append(float(pnl / rd))
            open_t = None
            continue
        open_t = {"d": d, "entry": fill, "stop": stop, "target": target}

    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    run = peak = dd = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {
        "mode": mode, "n_trades": len(pnls),
        "W": sum(1 for p in pnls if p > 0), "L": sum(1 for p in pnls if p <= 0),
        "PF": round(pf, 3), "WR_pct": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 1) if pnls else None,
        "E_R": round(float(np.mean(rs)), 4) if rs else 0.0,
        "pnl": round(float(sum(pnls)), 2), "maxDD": round(dd, 2),
        "n_signals": n_sig, "stale_fill_pct": round(100.0 * n_stale / n_sig, 1) if n_sig else None,
        "gapout_n": n_gapout,
        "req_gap_mean_pts": round(float(np.mean(gaps)), 2) if gaps else None,
        "req_gap_absmean_pts": round(float(np.mean(np.abs(gaps))), 2) if gaps else None,
        "req_gap_p90_abs_pts": round(float(np.percentile(np.abs(gaps), 90)), 2) if gaps else None,
        "cost_model": COST_MODEL_VERSION,
        "span_days": int((bars["ts"].iloc[-1] - bars["ts"].iloc[0]).total_seconds() // 86400),
        "n_bars": int(len(bars)),
    }


def main():
    conn = ro_conn()
    targets = json.loads(sys.argv[1]) if len(sys.argv) > 1 else [
        {"active_id": 4599, "symbol": "MNQ", "csid": 256, "tf": "15m"},
        {"active_id": 4678, "symbol": "MNQ", "csid": 270, "tf": "5m"},
    ]
    out = {"targets": [], "cost_model": COST_MODEL_VERSION}
    for t in targets:
        sym = t["symbol"]
        rec = dict(t)
        bars = bar_archive.read_archive(sym, t["tf"])
        if bars is None or bars.empty:
            rec["error"] = "ARCHIVE_MISSING"
            out["targets"].append(rec); continue
        bars = bars.reset_index(drop=True)
        code = code_for(conn, t["csid"])
        if not code:
            rec["error"] = "NO_CODE"
            out["targets"].append(rec); continue
        params = params_for(conn, t["active_id"])
        rec["params"] = params
        rec["as_promoted"] = replay(code, bars, params, sym, "signal_price")
        rec["live_fill_model"] = replay(code, bars, params, sym, "market_open")
        out["targets"].append(rec)
        print(json.dumps(rec, indent=2, default=str), flush=True)
        with open("outputs/fair_entry_remeasure_20261022.json", "w") as fh:
            json.dump(out, fh, indent=2, default=str)
    print("done")


if __name__ == "__main__":
    main()