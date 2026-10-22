"""STALE_ENTRY_PROBE_2026-10-22 -- decisive test of a suspected look-back fill.

Observation that prompted this
------------------------------
`silver_bullet_pdh_pdl_retest_*` (csid 229/270/368/403) restamps at
PF 6.325 / WR 81.7% on 120 days and 1,221 trades (MCL 5m). A 2R target with an
82% win rate over 1,221 independent trades is not a live edge; it is a
mechanical artifact. The fill model is NOT the cause -- `run_custom_backtest_on_bars`
uses `target_won = hit_target and not hit_stop`, i.e. STOP PRIORITY, which is
conservative and would bias results DOWN.

The suspect is the SIGNAL, not the fill. The strategy returns:

    entry_idx = k + 1                     # k < last, so the entry bar is in the PAST
    entry     = o[entry_idx]              # entry priced at an already-closed bar
    stop      = min(l[j], l[last]) - buf  # stop anchored to the CURRENT signal bar
    if risk >= 0.5 * tick                 # the ONLY size guard: half a tick

If price has already travelled in favour between `entry_idx` and `last`, then
`entry - stop` shrinks toward the 0.5-tick floor while the 2R target stays
anchored to that same shrunken risk. The trade is then a few ticks from its
target and wins almost mechanically.

Hypothesis: the high-PF envelopes of this family are a look-back fill artifact,
and the win rate is produced by micro-risk trades, not by market edge.

The test
--------
Run each strategy through the SAME production engine, changing only the entry:

  A BASELINE    registered code, unchanged                      (expect the known PF)
  B FAIRCLOSE   entry = c[last], the signal bar's own close      (decision-time achievable)
  C MINRISK     baseline entry, but require risk >= min_risk_ticks

WHY NOT o[last+1]: the engine calls `evaluate(bars[:i+1])`, so a strategy NEVER
sees the next bar -- indexing o[last+1] raises and the signal is swallowed
(that variant returned n=0). The entry must therefore be a price already in
hand. The two legitimate choices are the signal bar's open or its close; the
close is the realistic fill for a signal generated at bar close.

If A is high and B is ~1, the envelope is a look-back fill artifact.

READ-ONLY with respect to the registry and account: sqlite opened mode=ro.
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
MIN_RISK_TICKS = 20.0   # 0.20 on MCL (tick 0.01); 5.00 on MNQ (tick 0.25)

TARGETS = [
    ("4835/4874", "MCL", "5m", 368),
    ("4634", "MNQ", "1h", 229),
    ("4693", "MNQ", "5m", 253),
    ("4794", "MGC", "5m", 280),
]


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def code_for(conn, csid):
    r = conn.execute("SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    return r[0] if r else ""


def intended_params(conn, csid, symbol, tf):
    """Prefer the params the row was actually promoted with."""
    row = conn.execute(
        "SELECT params_json FROM active_strategies WHERE symbol=? AND state='active' "
        "AND params_json LIKE ? ORDER BY id DESC LIMIT 1",
        (symbol, f'%{csid}%'),
    ).fetchone()
    p = json.loads((row[0] if row else "{}") or "{}")
    drop = {"slot_key", "architecture", "strategy_name", "symbol",
            "strategy_family", "execution_engine", "custom_strategy_id",
            "cost_model", "session_start_utc", "session_end_utc"}
    p = {k: v for k, v in p.items() if k not in drop}
    p.setdefault("timeframe", tf)
    return p


# ---------------------------------------------------------------- variants

VARIANT = '''
def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    roll = int(params.get("rolling_pdhl_bars", params.get("rolling_window", 24)))
    min_risk = float(params.get("min_risk_ticks", 0.5)) * tick
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh = pd.Series(h).rolling(roll, min_periods=8).max().shift(1).values
    pdl = pd.Series(l).rolling(roll, min_periods=8).min().shift(1).values
    last = n - 1
    if last < 5:
        return None
    if np.isnan(pdh[last]) or np.isnan(pdl[last]):
        return None
    if h[last] >= pdh[last] + break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] < pdh[j] and l[j] >= pdl[j]:
                for k in range(j + 1, last):
                    if l[k] > h[k - 2]:
                        {ENTRY_LONG}
                break
    if l[last] <= pdl[last] - break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] > pdh[j] and h[j] <= pdh[j]:
                for k in range(j + 1, last):
                    if h[k] < l[k - 2]:
                        {ENTRY_SHORT}
                break
    return None
'''

# B: entry taken at the signal bar's own CLOSE -- a price the strategy already
# holds at decision time. This is the honest fill; the registered code instead
# fills at o[k+1], an open that closed up to max_retest bars earlier.
FAIRCLOSE_LONG = '''
                        entry_idx = last
                        entry = c[last]
                        stop = min(l[j], l[last]) - stop_buf
                        risk = entry - stop
                        if risk >= min_risk:
                            return {"ts": ts_v[last], "direction": "long",
                                    "entry": entry, "stop": stop,
                                    "target": entry + risk * target_r}'''
FAIRCLOSE_SHORT = '''
                        entry_idx = last
                        entry = c[last]
                        stop = max(h[j], h[last]) + stop_buf
                        risk = stop - entry
                        if risk >= min_risk:
                            return {"ts": ts_v[last], "direction": "short",
                                    "entry": entry, "stop": stop,
                                    "target": entry - risk * target_r}'''

BASELINE_LONG = '''
                        entry_idx = k + 1
                        entry = o[entry_idx]
                        stop = min(l[j], l[last]) - stop_buf
                        risk = entry - stop
                        if risk >= min_risk:
                            return {"ts": ts_v[entry_idx], "direction": "long",
                                    "entry": entry, "stop": stop,
                                    "target": entry + risk * target_r}'''
BASELINE_SHORT = '''
                        entry_idx = k + 1
                        entry = o[entry_idx]
                        stop = max(h[j], h[last]) + stop_buf
                        risk = stop - entry
                        if risk >= min_risk:
                            return {"ts": ts_v[entry_idx], "direction": "short",
                                    "entry": entry, "stop": stop,
                                    "target": entry - risk * target_r}'''

PREAMBLE = "import numpy as np\nimport pandas as pd\n"


def build(entry_long: str, entry_short: str) -> str:
    """Render the silver-bullet variant with a swappable entry rule."""
    return PREAMBLE + VARIANT.replace("{ENTRY_LONG}", entry_long).replace(
        "{ENTRY_SHORT}", entry_short)


def slim(res):
    n = int(res.get("n_trades", 0) or 0)
    w = int(res.get("n_wins", 0) or 0)
    return {
        "n": n, "W": w, "L": int(res.get("n_losses", 0) or 0),
        "pf": round(float(res.get("pf", 0.0) or 0.0), 3),
        "wr": round(100.0 * w / n, 1) if n else 0.0,
        "e_r": round(float(res.get("expectancy_r", 0.0) or 0.0), 3),
        "usd": round(float(res.get("total_pnl", 0.0) or 0.0), 2),
        "span_d": res.get("bars_span_days"),
        "cm": res.get("cost_model", COST_MODEL_VERSION),
        "err": res.get("error"),
    }


def run(code, bars, params, sym):
    t0 = time.time()
    r = run_custom_backtest_on_bars(code=code, bars=bars, params=params,
                                    symbol=sym, qty=1, timeout_s=900)
    out = slim(r)
    out["wall_s"] = round(time.time() - t0, 1)
    return out


def main():
    conn = ro_conn()
    report = {"cost_model": COST_MODEL_VERSION, "min_risk_ticks": MIN_RISK_TICKS,
              "rows": []}
    for label, sym, tf, csid in TARGETS:
        base = code_for(conn, csid)
        if not base:
            print(f"[{label}] no code for csid {csid}")
            continue
        bars = bar_archive.read_archive(sym, tf)
        if bars is None or bars.empty:
            print(f"[{label}] archive MISSING {sym} {tf}")
            continue
        bars = bars.reset_index(drop=True)
        params = intended_params(conn, csid, sym, tf)
        params["min_risk_ticks"] = MIN_RISK_TICKS
        rec = {"row": label, "symbol": sym, "tf": tf, "csid": csid,
               "archive_bars": int(len(bars)),
               "archive_span_d": int((bars["ts"].iloc[-1] - bars["ts"].iloc[0]).days),
               "params": params}
        rec["A_baseline"] = run(base, bars, params, sym)
        print(f"[{label}] {sym} {tf} A_baseline {rec['A_baseline']}", flush=True)
        rec["B_fairclose"] = run(build(FAIRCLOSE_LONG, FAIRCLOSE_SHORT),
                                 bars, params, sym)
        print(f"[{label}] {sym} {tf} B_fairclose {rec['B_fairclose']}", flush=True)
        rec["C_minrisk"] = run(build(BASELINE_LONG, BASELINE_SHORT),
                               bars, params, sym)
        print(f"[{label}] {sym} {tf} C_minrisk  {rec['C_minrisk']}", flush=True)
        report["rows"].append(rec)
        with open("outputs/stale_entry_probe_20261022.json", "w") as fh:
            json.dump(report, fh, indent=2, default=str)

    print("\n=== STALE-ENTRY PROBE SUMMARY ===")
    print(f"{'row':>10} {'sym':>4} {'tf':>3} | {'A base':>22} | {'B fairclose':>22} | {'C minrisk':>22}")
    for r in report["rows"]:
        a, b, c = r["A_baseline"], r["B_fairclose"], r["C_minrisk"]
        print(f"{r['row']:>10} {r['symbol']:>4} {r['tf']:>3} | "
              f"pf={a['pf']:>6} n={a['n']:<5} wr={a['wr']:>5} | "
              f"pf={b['pf']:>6} n={b['n']:<5} wr={b['wr']:>5} | "
              f"pf={c['pf']:>6} n={c['n']:<5} wr={c['wr']:>5}")


if __name__ == "__main__":
    main()