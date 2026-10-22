"""STALE_FILL_SCAN_2026-10-22 -- code-agnostic measurement of the retro-fill defect.

Why this script exists
----------------------
`stale_entry_probe_20261022.py` proved the defect by REWRITING the silver-bullet
entry rule. That test is only valid for strategies whose code has that shape
(csid 229 / 368, which carry the two largest claimed envelopes in the book).
It is NOT valid for 4693 / 4794, whose registered code is a different
architecture -- measuring those with the silver-bullet template would have been
a category error.

So measure the defect WITHOUT rewriting anything. The engine's contract is:
`evaluate(bars[:i+1])` returns a signal, and the engine resolves the trade from
bar i+1. Therefore:

    if signal["ts"] < bars.iloc[i]["ts"]   ->  the fill price is from a
                                                bar that had ALREADY CLOSED
                                                when the signal was emitted.

That is a retro-fill, and it is measurable for ANY strategy. We report, per
strategy:
    n_signals        total signals emitted
    stale_n          signals whose entry ts predates the decision bar
    stale_pct        stale_n / n_signals
    stale_mean_bars  mean number of bars the fill price predates the decision

A strategy with stale_pct materially above 0 has a backtest fill it could not
have obtained. This is the number that decides whether an envelope is real.

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


def scan(code, bars, params, step=1):
    """Replay evaluate() bar by bar; measure how far in the past each fill is."""
    fn, _meta = _compile_and_load(code)
    if fn is None:
        return {"error": "compile_failed"}
    ts = bars["ts"].values
    ts_dt = pd.to_datetime(pd.Series(ts), utc=True, errors="coerce")
    n = len(bars)
    n_sig = 0
    stale_n = 0
    stale_bars = []
    # emulate the engine's "one trade at a time" gate coarsely: a signal on any
    # bar is counted, which is the most generous possible reading of the defect.
    for i in range(2, n, step):
        try:
            sig = fn(bars.iloc[: i + 1], dict(params))
        except Exception:
            continue
        if not isinstance(sig, dict):
            continue
        if "entry" not in sig or "ts" not in sig:
            continue
        n_sig += 1
        try:
            sig_ts = pd.to_datetime(sig["ts"], utc=True)
        except Exception:
            continue
        if pd.isna(sig_ts):
            continue
        delta = (ts_dt.iloc[i] - sig_ts).total_seconds()
        # >90s means the fill bar is not the decision bar (5m+ timeframes).
        if delta > 90:
            stale_n += 1
            stale_bars.append(delta)
    out = {
        "n_signals": n_sig,
        "stale_n": stale_n,
        "stale_pct": round(100.0 * stale_n / n_sig, 1) if n_sig else None,
    }
    if stale_bars:
        out["stale_mean_min"] = round(float(np.mean(stale_bars)) / 60.0, 1)
        out["stale_max_min"] = round(float(np.max(stale_bars)) / 60.0, 1)
    return out


def main():
    conn = ro_conn()
    # every ACTIVE strategy that runs on the custom engine, with its csid
    acts = conn.execute(
        "SELECT id, symbol, params_json FROM active_strategies WHERE state='active'"
    ).fetchall()
    targets = []
    for aid, sym, pj in acts:
        try:
            p = json.loads(pj or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        cs = p.get("custom_strategy_id")
        if cs:
            targets.append((aid, sym, int(cs), p.get("timeframe", "5m")))
    # de-dup on (symbol, csid, tf) -- we measure the STRATEGY, not each row
    seen, uniq = set(), []
    for aid, sym, cs, tf in targets:
        key = (sym, cs, tf)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((aid, sym, cs, tf))

    report = {"rows": [], "note": "stale_pct = share of signals whose fill price "
                                  "predates the decision bar (retro-fill)"}
    print(f"scanning {len(uniq)} distinct (symbol, csid, tf) custom actives",
          flush=True)

    for aid, sym, cs, tf in uniq:
        tf_n = "1h" if str(tf).lower() in ("1h", "60") else (
               "15m" if str(tf).lower() in ("15m", "15") else "5m")
        try:
            bars = bar_archive.read_archive(sym, tf_n)
        except Exception as exc:  # noqa: BLE001
            rec = {"active_id": aid, "symbol": sym, "csid": cs, "tf": tf_n,
                   "error": f"archive: {exc}"}
            report["rows"].append(rec)
            continue
        if bars is None or bars.empty:
            rec = {"active_id": aid, "symbol": sym, "csid": cs, "tf": tf_n,
                   "error": "ARCHIVE_MISSING"}
            report["rows"].append(rec)
            continue
        bars = bars.reset_index(drop=True)
        code = code_for(conn, cs)
        if not code:
            rec = {"active_id": aid, "symbol": sym, "csid": cs, "tf": tf_n,
                   "error": "NO_CODE"}
            report["rows"].append(rec)
            continue
        t0 = time.time()
        # step=2 halves runtime; still ~11k decision points
        res = scan(code, bars, params_for(conn, aid), step=2)
        rec = {"active_id": aid, "symbol": sym, "csid": cs, "tf": tf_n,
               "n_bars": int(len(bars)), "wall_s": round(time.time() - t0, 1)}
        rec.update(res)
        report["rows"].append(rec)
        print(f"  a{aid} {sym} {tf_n} csid={cs}: n_sig={res.get('n_signals')} "
              f"stale={res.get('stale_pct')}% mean={res.get('stale_mean_min')}min "
              f"{res.get('error','')}", flush=True)
        with open("outputs/stale_fill_scan_20261022.json", "w") as fh:
            json.dump(report, fh, indent=2, default=str)

    ok = [r for r in report["rows"] if r.get("stale_pct") is not None]
    print("\n=== SUMMARY ===")
    if ok:
        sp = sorted(r["stale_pct"] for r in ok)
        med = sp[len(sp) // 2]
        bad = [r for r in ok if r["stale_pct"] > 0]
        print(f"measured {len(ok)} strategies; median stale_pct = {med}%")
        print(f"strategies with ANY retro-fill: {len(bad)}")
        for r in sorted(bad, key=lambda x: -x["stale_pct"])[:25]:
            print(f"   a{r['active_id']:>5} {r['symbol']:>4} {r['tf']:>3} "
                  f"csid={r['csid']:<4} stale={r['stale_pct']:>5}% "
                  f"mean={r.get('stale_mean_min')}min")
    with open("outputs/stale_fill_scan_20261022.json", "w") as fh:
        json.dump(report, fh, indent=2, default=str)


if __name__ == "__main__":
    main()