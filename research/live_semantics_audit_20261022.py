"""LIVE_SEMANTICS_AUDIT_2026-10-22 -- task #2867.

Answers, code-agnostically and for EVERY active custom-engine row:
  (A) %_stale_fills            -- does the registered code fill on a closed bar?
  (C) what the envelope is worth if we trade it the way we ACTUALLY trade it.

WHY THIS IS THE RIGHT FAIR-ENTRY MODEL
--------------------------------------
Finding 07f3a4c2 measured fair entry as `entry = c[last]` with the strategy's
own stop/target carried over. That is close, but it is not what the live path
does. The live path is `src/broker/ninjatrader.py:place_bracket`:

    1. PLACE entry as a MARKET order (order_type="MARKET", limit_price=0.0)
       -> the fill price is the market, NEVER the signal price.
    2. poll the actual fill -> entry_fill_price
    3. stop_dist   = abs(entry_ref - stop_price)      # entry_ref = sig["entry"]
       target_dist = abs(target_price - entry_ref)
       stop   = fill -/+ stop_dist                    # distances PRESERVED
       target = fill +/- target_dist                  # re-anchored to the fill

So at live time the stale `entry` price contributes ONLY its distance to stop
and target; the fill itself is the decision bar's market price. That is exactly
what this script simulates: entry := close of the decision bar, with the
strategy's intended risk/reward distances carried across unchanged.

This is code-agnostic -- it wraps the strategy's OWN evaluate() -- so it works
on all 174 actives and needs no per-strategy rewrite (the defect is in the
FILL RULE, not in any one template).

RUNS
----
A_baseline : registered code, unchanged. The engine's own stale_fill_pct
             counter (added under 07f3a4c2) measures contamination here.
B_live     : same signal, live fill semantics. This is the honest envelope.

READ-ONLY: sqlite mode=ro. No registry or account writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bar_archive  # noqa: E402
from src.research.custom_strategy_runtime import run_custom_backtest_on_bars  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION  # noqa: E402

DB = "judas_crew.db"
OUT = "outputs/live_semantics_audit_20261022.json"
PER_RUN_TIMEOUT = int(os.environ.get("AUDIT_TIMEOUT", "240"))
LEGAL = {"MGC", "MNQ", "MCL", "ZF", "6J"}

# The wrapper. Calls the strategy's own evaluate(), then re-derives the fill
# exactly as ninjatrader.place_bracket does.
WRAPPER = '''

# ---- live-semantics fill wrapper (task #2867) --------------------------
_audit_original_evaluate = evaluate


def _audit_live_fill(bars, params):
    import numpy as _np
    _sig = _audit_original_evaluate(bars, params)
    if _sig is None or not isinstance(_sig, dict):
        return None
    try:
        _dir = str(_sig.get("direction", "")).lower()
        _entry = float(_sig["entry"]); _stop = float(_sig["stop"])
        _target = float(_sig["target"])
    except Exception:
        return None
    if _dir not in ("long", "short"):
        return None
    if bars is None or len(bars) == 0 or "close" not in bars.columns:
        return None
    _last = len(bars) - 1
    _fill = float(bars["close"].iloc[_last])
    if _np.isnan(_fill) or _np.isinf(_fill) or _fill <= 0:
        return None
    _sd = abs(_entry - _stop)
    _td = abs(_target - _entry)
    _out = dict(_sig)
    if _dir == "long":
        _out["stop"] = _fill - _sd
        _out["target"] = _fill + _td
    else:
        _out["stop"] = _fill + _sd
        _out["target"] = _fill - _td
    _out["entry"] = _fill
    try:
        _out["ts"] = bars["ts"].iloc[_last]
    except Exception:
        pass
    return _out


evaluate = _audit_live_fill
'''

DROP = {"slot_key", "architecture", "strategy_name", "symbol",
        "strategy_family", "execution_engine", "custom_strategy_id",
        "cost_model", "session_start_utc", "session_end_utc"}


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def slim(res):
    n = int(res.get("n_trades", 0) or 0)
    w = int(res.get("n_wins", 0) or 0)
    return {
        "n": n, "W": w, "L": int(res.get("n_losses", 0) or 0),
        "pf": round(float(res.get("pf", 0.0) or 0.0), 3),
        "wr": round(100.0 * w / n, 1) if n else 0.0,
        "e_r": round(float(res.get("expectancy_r", 0.0) or 0.0), 3),
        "usd": round(float(res.get("total_pnl", 0.0) or 0.0), 2),
        "maxdd": round(float(res.get("max_drawdown", 0.0) or 0.0), 2),
        "span_d": res.get("bars_span_days"),
        "stale_pct": res.get("stale_fill_pct"),
        "cm": res.get("cost_model", COST_MODEL_VERSION),
        "err": res.get("error"),
    }


def run(code, bars, params, sym):
    t0 = time.time()
    try:
        r = run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                        symbol=sym, qty=1,
                                        timeout_s=PER_RUN_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return {"n": 0, "err": f"{type(exc).__name__}: {exc}",
                "wall_s": round(time.time() - t0, 1)}
    out = slim(r)
    out["wall_s"] = round(time.time() - t0, 1)
    return out


def main():
    conn = ro_conn()
    actives = conn.execute(
        "SELECT id, symbol, strategy_family, params_json, metrics_json "
        "FROM active_strategies WHERE state='active' ORDER BY id").fetchall()

    by_combo = defaultdict(list)
    for sid, sym, fam, pj, mj in actives:
        try:
            p = json.loads(pj or "{}")
        except Exception:
            p = {}
        cid = p.get("custom_strategy_id")
        if not cid:
            continue
        try:
            cid = int(cid)
        except Exception:
            continue
        tf = str(p.get("timeframe", "1h"))
        by_combo[(cid, sym, tf)].append((sid, fam, p, mj))

    report = {"cost_model": COST_MODEL_VERSION, "rows": [],
              "n_actives_seen": len(actives),
              "n_actives_custom": sum(len(v) for v in by_combo.values()),
              "n_unique_combos": len(by_combo)}

    # Priority: the two broker-confirmed earners first, then the three
    # just-promoted rows (csid 436/437/438), then everything else.
    def prio(k):
        cid = k[0]
        if cid in (256, 270):
            return 0
        if cid in (436, 437, 438):
            return 1
        return 2

    combos = sorted(by_combo.keys(), key=prio)
    cache = {}

    for idx, (cid, sym, tf) in enumerate(combos, 1):
        sids, fams, p0, _ = by_combo[(cid, sym, tf)][0]
        if sym not in LEGAL:
            continue
        if (sym, tf) not in cache:
            try:
                b = bar_archive.read_archive(sym, tf)
                cache[(sym, tf)] = b.reset_index(drop=True) if (
                    b is not None and not b.empty) else None
            except Exception:
                cache[(sym, tf)] = None
        bars = cache[(sym, tf)]
        row = {"csid": cid, "symbol": sym, "tf": tf,
               "active_ids": [a[0] for a in by_combo[(cid, sym, tf)]],
               "n_actives": len(by_combo[(cid, sym, tf)])}
        if bars is None:
            row["status"] = "NO_ARCHIVE"
            report["rows"].append(row)
            print(f"[{idx}/{len(combos)}] csid={cid} {sym} {tf} NO_ARCHIVE",
                  flush=True)
            continue
        code_r = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (cid,)).fetchone()
        code = code_r[0] if code_r else ""
        if not code:
            row["status"] = "NO_CODE"
            report["rows"].append(row)
            continue
        params = {k: v for k, v in p0.items() if k not in DROP}
        params.setdefault("timeframe", tf)
        row["params"] = params
        row["archive_bars"] = int(len(bars))
        row["archive_span_d"] = int((bars["ts"].iloc[-1] - bars["ts"].iloc[0]).days)

        row["A_baseline"] = run(code, bars, params, sym)
        row["B_live"] = run(code + WRAPPER, bars, params, sym)
        report["rows"].append(row)
        a, bl = row["A_baseline"], row["B_live"]
        print(f"[{idx}/{len(combos)}] csid={cid} {sym:>3} {tf:>3} "
              f"A: pf={a.get('pf')} n={a.get('n')} stale={a.get('stale_pct')}% "
              f"| B_live: pf={bl.get('pf')} n={bl.get('n')} wr={bl.get('wr')}% "
              f"${bl.get('usd')}", flush=True)
        with open(OUT, "w") as fh:
            json.dump(report, fh, indent=2, default=str)

    with open(OUT, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\nwrote {OUT} rows={len(report['rows'])}")


if __name__ == "__main__":
    main()