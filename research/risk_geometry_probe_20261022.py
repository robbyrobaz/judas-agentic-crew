"""WHY-IS-THE-WIN-RATE-90% probe for #2861.

The restamp showed MCL 5m csid368 (PF 6.33, n=1221, WR 81.7%) and MNQ 1h
csid229 (PF 5.06, n=163, WR 82.8%) do NOT deflate when the window widens --
they stay pinned at an 80-90% win rate.  Finding 6511 says a genuine 2R edge
reads PF 1.2-2.5 / WR 45-60%.  Our own broker-confirmed book reads WR 36-40%.

An 83% win rate on a 2R target is not a strategy being good.  The usual cause is
that the STOP is so tight it sits inside the spread, so on historical bars
price essentially never reaches it while the 2R target is only a few ticks away
and gets tagged by ordinary noise.  That is a fill-model artifact, not edge,
and it would survive ANY window -- which is exactly why the split-half test
cannot catch it.

This measures the risk geometry directly: for every signal the strategy emits,
how many ticks (and how many dollars) is the stop, and how many ticks is the
target.  Then it compares the stop against the instrument's own tick/spread.

READ-ONLY. sqlite opened mode=ro. No registry or account writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402

DB = "judas_crew.db"
DROP = {"slot_key", "architecture", "strategy_name", "symbol", "strategy_family",
        "execution_engine", "custom_strategy_id", "cost_model",
        "session_start_utc", "session_end_utc"}

TARGETS = [
    ("4835/4874", "MCL", "5m", 368, 4874),
    ("4634",      "MNQ", "1h", 229, 4634),
    ("4693",      "MNQ", "5m", 253, 4693),
]


def probe(label, sym, tf, csid, act_id, conn):
    code = conn.execute("SELECT code FROM custom_strategies WHERE id=?",
                        (csid,)).fetchone()[0]
    p = json.loads(conn.execute(
        "SELECT params_json FROM active_strategies WHERE id=?",
        (act_id,)).fetchone()[0] or "{}")
    params = {k: v for k, v in p.items() if k not in DROP}
    fn, meta = _compile_and_load(code)
    if fn is None:
        return {"row": label, "error": meta.get("error")}

    bars = bar_archive.read_archive(sym, tf)
    spec = _CONTRACT_SPECS.get(sym.upper(), {})
    tick = float(spec.get("tick") or 0.01)
    tick_value = float(spec.get("tick_value") or 1.0)
    dpp = tick_value / tick

    stop_ticks, tgt_ticks, rrs = [], [], []
    n = len(bars)
    # The engine forbids overlapping trades; mirror that so the sample matches.
    blocked_until = -1
    for i in range(2, n):
        if i <= blocked_until:
            continue
        try:
            sig = fn(bars.iloc[: i + 1], params)
        except Exception:
            sig = None
        if not isinstance(sig, dict):
            continue
        d = str(sig.get("direction", "")).lower()
        if d not in ("long", "short"):
            continue
        try:
            entry, stop, target = float(sig["entry"]), float(sig["stop"]), float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        stop_ticks.append(risk / tick)
        tgt_ticks.append(abs(target - entry) / tick)
        rrs.append(abs(target - entry) / risk)
        blocked_until = i + 12  # ~1h hold cap; mirrors max_retest_bars scale

    if not stop_ticks:
        return {"row": label, "n_signals": 0}

    st = np.array(stop_ticks)
    tt = np.array(tgt_ticks)
    rr = np.array(rrs)
    return {
        "row": label, "symbol": sym, "tf": tf, "csid": csid,
        "n_signals": int(len(st)),
        "tick": tick, "tick_value": tick_value, "dollars_per_point": dpp,
        "stop_ticks": {"min": float(st.min()), "p10": float(np.percentile(st, 10)),
                       "median": float(np.median(st)), "p90": float(np.percentile(st, 90)),
                       "max": float(st.max()), "mean": round(float(st.mean()), 2)},
        "target_ticks_median": float(np.median(tt)),
        "rr_median": round(float(np.median(rr)), 3),
        "risk_usd_median": round(float(np.median(st) * tick_value), 2),
        "stop_within_3_ticks_pct": round(100.0 * float((st <= 3).mean()), 1),
        "stop_within_5_ticks_pct": round(100.0 * float((st <= 5).mean()), 1),
        "n_distinct_stop_ticks": int(len(set(np.round(st, 3)))),
        "top_stop_ticks": Counter(np.round(st, 2).tolist()).most_common(5),
    }


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = []
    for t in TARGETS:
        r = probe(*t, conn)
        out.append(r)
        print(json.dumps(r, indent=2, default=str), flush=True)
    with open("outputs/risk_geometry_20261022.json", "w") as fh:
        json.dump(out, fh, indent=2, default=str)


if __name__ == "__main__":
    main()