"""GAP_GATE_SWEEP_2026-10-22 -- is there an executable edge under the live fill model?

`fair_entry_remeasure_20261022.py` showed two MNQ envelopes with
``stale_fill_pct = 0.0`` collapsing from PF 4.57 -> 0.92 and PF 3.44 -> 0.83
when the fill is moved from the strategy's requested price to the market price
the live runtime actually crosses.

The mechanism is measurable without any simulation: the strategy asks for a
price 80-132 MNQ points away from the market (p90 ~ 185-270). The backtest
assumes we get it. We do not.

THE QUESTION THIS ANSWERS: is the collapse the *strategy* failing, or only the
*fill assumption*? Those need opposite responses:

  * strategy failed        -> retire the family
  * fill assumption failed -> add a proximity gate on the live signal and the
                              residual is a real, executable edge

Gate: keep a signal only when the requested entry is within ``max_gap_pts`` of
the market at the decision bar (signed adversely, i.e. abs distance). Everything
else is identical to `live_fill_model`: market fill at bar i+1's open, stop and
target distances re-anchored to the actual fill (exactly what
`NinjaTraderBroker.place_bracket` does with ``entry_ref``).

READ-ONLY: sqlite mode=ro, bars from the archive.
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


def run(code, bars, params, symbol, gates, timeout_s=900):
    """One pass over the bars; every gate evaluated from the same signals."""
    fn, meta = _compile_and_load(code)
    if fn is None:
        return {"error": meta.get("error")}
    from src.portfolio_runtime import _CONTRACT_SPECS
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    qty = int(params.get("qty", 1) or 1)

    book = {g: {"pnls": [], "rs": []} for g in gates}
    open_t = {g: None for g in gates}
    skipped = {g: 0 for g in gates}
    t0 = time.time()
    n = len(bars)
    for i in range(2, n):
        if time.time() - t0 > timeout_s:
            break
        for g in gates:
            ot = open_t[g]
            if ot is None:
                continue
            bar = bars.iloc[i]
            hs = ((ot["d"] == "long" and float(bar["low"]) <= ot["stop"]) or
                  (ot["d"] == "short" and float(bar["high"]) >= ot["stop"]))
            ht = ((ot["d"] == "long" and float(bar["high"]) >= ot["target"]) or
                  (ot["d"] == "short" and float(bar["low"]) <= ot["target"]))
            if hs or ht:
                entry = ot["entry"]; risk = abs(entry - ot["stop"]) or 1.0
                won = ht and not hs
                pts = abs(ot["target"] - entry) if won else -abs(entry - ot["stop"])
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=pts * dpp * qty,
                                           exit_reason="target" if won else "stop", qty=qty)
                book[g]["pnls"].append(float(pnl) if not isinstance(pnl, (list, tuple)) else float(pnl[0]))
                rd = risk * dpp * qty
                book[g]["rs"].append(float(pnl / rd) if rd else 0.0)
                open_t[g] = None

        if all(open_t[g] is not None for g in gates):
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
        if i + 1 >= n:
            continue
        mkt = float(bars.iloc[i]["close"])
        gap = (req - mkt) if d == "long" else (mkt - req)
        nxt = bars.iloc[i + 1]
        fill = float(nxt["open"])
        stop_d = abs(req - st); tgt_d = abs(tg - req)
        for g in gates:
            if open_t[g] is not None:
                continue
            if abs(gap) > g:
                skipped[g] += 1
                continue
            if d == "long":
                stop = fill - stop_d; target = fill + tgt_d
            else:
                stop = fill + stop_d; target = fill - tgt_d
            if (d == "long" and fill <= stop) or (d == "short" and fill >= stop):
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=-stop_d * dpp * qty,
                                           exit_reason="stop", qty=qty)
                book[g]["pnls"].append(float(pnl) if not isinstance(pnl, (list, tuple)) else float(pnl[0]))
                book[g]["rs"].append(float(pnl / ((stop_d * dpp * qty) or 1.0)))
                continue
            open_t[g] = {"d": d, "entry": fill, "stop": stop, "target": target}

    out = {}
    for g in gates:
        p = book[g]["pnls"]; rs = book[g]["rs"]
        gw = sum(x for x in p if x > 0); gl = -sum(x for x in p if x < 0)
        pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
        run = peak = dd = 0.0
        for x in p:
            run += x; peak = max(peak, run); dd = max(dd, peak - run)
        out[str(g)] = {
            "gate_pts": g, "n": len(p),
            "W": sum(1 for x in p if x > 0), "L": sum(1 for x in p <= 0),
            "PF": round(pf, 3),
            "WR": round(100.0 * sum(1 for x in p if x > 0) / len(p), 1) if p else None,
            "E_R": round(float(np.mean(rs)), 4) if rs else 0.0,
            "pnl": round(float(sum(p)), 2), "maxDD": round(dd, 2),
            "signals_skipped_by_gate": skipped[g],
        }
    return out


def main():
    conn = ro_conn()
    spec = json.loads(sys.argv[1])
    sym, tf, csid, aid = spec["symbol"], spec["tf"], spec["csid"], spec["active_id"]
    gates = spec.get("gates", [2, 4, 8, 16, 32, 64, 128])
    bars = bar_archive.read_archive(sym, tf).reset_index(drop=True)
    code = code_for(conn, csid)
    params = params_for(conn, aid)
    res = run(code, bars, params, sym, gates)
    res["_meta"] = {"symbol": sym, "tf": tf, "csid": csid, "active_id": aid,
                    "span_days": int((bars["ts"].iloc[-1] - bars["ts"].iloc[0]).total_seconds() // 86400),
                    "n_bars": int(len(bars)), "params": params,
                    "cost_model": COST_MODEL_VERSION}
    fn = os.path.basename(sys.argv[0]).replace(".py", "")
    tag = spec.get("tag", f"{sym}_{tf}_csid{csid}")
    with open(f"outputs/gap_gate_{tag}.json", "w") as fh:
        json.dump(res, fh, indent=2, default=str)
    print(json.dumps(res, indent=2, default=str))


if __name__ == "__main__":
    main()