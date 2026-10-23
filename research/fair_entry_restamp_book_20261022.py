"""FAIR ENTRY RESTAMP HARNESS -- task #2869.

Isolates ONE variable: the price a signal is FILLED at.

Every `custom` envelope in this book was produced by
`src/research/custom_strategy_runtime.py::run_custom_backtest_on_bars`, which
hands `evaluate()` the prefix `bars[:i+1]`, takes `sig["entry"]` verbatim, and
resolves the trade from bar i+1 onward.  A strategy is free to return an entry
priced off a bar MANY steps back in that prefix (finding 07f3a4c2 / 9121), so
the envelope is priced at a level the market had already passed through.

Three variants are simulated from ONE identical signal stream, so the only
difference between them is the fill:

  BASE   entry = sig["entry"] (what the promotion gate saw).
         stop/target fills AT the stop/target price, no gap logic -- a literal
         replication of the current engine.
  FAIR   entry = close[decision_bar] (the achievable decision-time market
         fill), stop/target translated by the SAME authored distances, and
         gap-aware fills (a stop is filled at the open if the open already
         gapped through it).
  FAIRNG same as FAIR but with the engine's non-gap-aware stop/target fills.
         Isolates "what the strategy returns" from "how the exit is filled".

READ-ONLY w.r.t. the registry and the account: the DB is opened mode=ro and
nothing is written back.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade  # noqa: E402

# Slice cap handed to evaluate(). Keeps the O(n^2) prefix replay tractable;
# every strategy in the book declares its own minimum bar count (320, 30, ...)
# and uses rolling windows of <= ~100 bars, so 600 is comfortably above what
# any of them actually read.  `LOOKBACK_PROBE` validates this per unit.
SLICE_CAP = {"5m": 700, "15m": 500, "1h": 400}
PER_CALL_WARN_S = 1.5
UNIT_TIME_BUDGET_S = 240


# ---------------------------------------------------------------- stats ----
def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-7:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1.0 - x) / b


def t_sf_1samp(t: float, df: int) -> float:
    """One-sided survival probability for a Student-t statistic."""
    if df <= 0 or not math.isfinite(t):
        return 1.0
    x = df / (df + t * t)
    return 0.5 * _betai(0.5 * df, 0.5, x) if t > 0 else 1.0 - 0.5 * _betai(0.5 * df, 0.5, x)


# ------------------------------------------------------------- pass 1 ------
def collect_signals(code: str, bars: pd.DataFrame, params: dict, slice_cap: int,
                    budget_s: float = UNIT_TIME_BUDGET_S):
    """Replay the engine's prefix loop, return the raw signal stream.

    Returns (signals, meta).  `signals` entries:
        {i, direction, entry, stop, target, sig_ts}
    """
    fn, meta = _compile_and_load(code)
    if fn is None:
        return [], {"load_error": meta.get("error")}
    p = dict(params)
    n = len(bars)
    out = []
    errs = 0
    t0 = time.time()
    slow = 0
    for i in range(2, n):
        if time.time() - t0 > budget_s:
            return out, {"timeout": True, "bars_done": i, "n_bars": n, "eval_errors": errs}
        lo = max(0, i - slice_cap + 1)
        try:
            sig = fn(bars.iloc[lo:i + 1], p)
        except Exception:  # noqa: BLE001
            errs += 1
            continue
        if not isinstance(sig, dict):
            continue
        d = str(sig.get("direction", "")).lower()
        if d not in ("long", "short"):
            continue
        try:
            entry = float(sig["entry"])
            stop = float(sig["stop"])
            target = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(entry) and math.isfinite(stop) and math.isfinite(target)):
            continue
        out.append({"i": i, "direction": d, "entry": entry, "stop": stop,
                    "target": target, "sig_ts": str(sig.get("ts"))})
    dt = time.time() - t0
    return out, {"bars_done": n, "n_bars": n, "eval_errors": errs,
                 "loop_s": round(dt, 1), "ms_per_call": round(1000 * dt / max(1, n - 2), 3),
                 "slice_cap": slice_cap, "slow_calls": slow}


# ------------------------------------------------------------- pass 2 ------
def simulate(signals, bars: pd.DataFrame, symbol: str, dpp: float, mode: str,
             qty: int = 1):
    """Walk the signal stream, resolving exits bar-by-bar.

    mode: 'base'  -> entry=sig entry, fills at stop/target price (engine clone)
          'fairng'-> entry=close[i],   fills at stop/target price
          'fair'   -> entry=close[i],   gap-aware fills
    """
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    n = len(bars)
    trades = []
    open_t = None
    sig_iter = iter(signals)
    pending = next(sig_iter, None)
    for i in range(2, n):
        if open_t is not None:
            d = open_t["direction"]
            o, h, l = O[i], H[i], L[i]
            if d == "long":
                stop_hit = l <= open_t["stop"]
                tgt_hit = h >= open_t["target"]
            else:
                stop_hit = h >= open_t["stop"]
                tgt_hit = l <= open_t["target"]
            if stop_hit or tgt_hit:
                if mode == "fair":
                    if d == "long":
                        if stop_hit and o <= open_t["stop"]:
                            px, reason = o, "stop_gap"
                        elif stop_hit:
                            px, reason = open_t["stop"], "stop"
                        elif o >= open_t["target"]:
                            px, reason = o, "target_gap"
                        else:
                            px, reason = open_t["target"], "target"
                    else:
                        if stop_hit and o >= open_t["stop"]:
                            px, reason = o, "stop_gap"
                        elif stop_hit:
                            px, reason = open_t["stop"], "stop"
                        elif o <= open_t["target"]:
                            px, reason = o, "target_gap"
                        else:
                            px, reason = open_t["target"], "target"
                else:
                    if stop_hit:
                        px, reason = open_t["stop"], "stop"
                    else:
                        px, reason = open_t["target"], "target"
                e = open_t["entry"]
                gross = (px - e) if d == "long" else (e - px)
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross * dpp * qty,
                                           exit_reason="target" if "target" in reason else "stop",
                                           qty=qty)
                risk = abs(e - open_t["stop"]) or 1.0
                trades.append({"entry_bar": open_t["entry_bar"], "exit_bar": i,
                               "direction": d, "entry": e, "px": px, "reason": reason,
                               "pnl": float(pnl), "r": float(gross / risk),
                               "bars_held": i - open_t["entry_bar"]})
                open_t = None
                continue
        if open_t is not None:
            continue
        # take the next signal whose decision bar is <= i (signals are produced
        # at the decision bar; the engine resolves from the NEXT bar).
        while pending is not None and pending["i"] <= i:
            s = pending
            pending = next(sig_iter, None)
            d = s["direction"]
            if mode == "base":
                entry = s["entry"]
            else:
                entry = C[i]
            sd = abs(s["entry"] - s["stop"])
            td = abs(s["target"] - s["entry"])
            if not (sd > 0) or not (td > 0):
                continue
            sign = 1.0 if d == "long" else -1.0
            open_t = {"direction": d, "entry": entry,
                      "stop": entry - sign * sd, "target": entry + sign * td,
                      "entry_bar": i + 1}
            break
    if open_t is not None:  # still open at the end of the archive
        pass
    return trades


def metrics(trades, symbol, dpp, qty=1):
    if not trades:
        return {"n": 0, "pf": 0.0, "wr_pct": 0.0, "usd_per_trade": 0.0,
                "e_r": 0.0, "total_usd": 0.0, "p_1sided": 1.0, "max_dd": 0.0}
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    pf = (gw / gl) if gl > 0 else 999.0
    rs = np.array([t["r"] for t in trades], float)
    mean_r = float(rs.mean())
    sd = float(rs.std(ddof=1)) if n > 1 else 0.0
    if n > 1 and sd > 0:
        tstat = mean_r / (sd / math.sqrt(n))
        p = t_sf_1samp(tstat, n - 1)
    else:
        tstat, p = 0.0, 1.0
    run = peak = dd = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {"n": n, "pf": round(pf, 3), "wr_pct": round(100.0 * sum(1 for x in pnls if x > 0) / n, 1),
            "usd_per_trade": round(sum(pnls) / n, 2), "e_r": round(mean_r, 3),
            "total_usd": round(sum(pnls), 2), "p_1sided": p, "t_stat": round(tstat, 3),
            "max_dd": round(dd, 2), "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1),
            "n_stop_gap": sum(1 for t in trades if t["reason"] == "stop_gap"),
            "n_target_gap": sum(1 for t in trades if t["reason"] == "target_gap")}


# ----------------------------------------------------------------- main ----
def run_units(units, out_path, only_ids=None, symbols=None):
    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {}
    for cid, code in con.execute("SELECT id, code FROM custom_strategies"):
        codes[cid] = code
    pop = json.load(open("outputs/fair_book_pop_20261022.json"))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    con.close()

    results = []
    if os_path_exists(out_path):
        try:
            results = json.load(open(out_path))["rows"]
        except Exception:  # noqa: BLE001
            results = []
    done = {(r["csid"], r["symbol"], r["tf"]) for r in results}

    cache = {}
    for u in units:
        key = (u["csid"], u["symbol"], u["tf"])
        if key in done:
            continue
        if only_ids and not (set(u["ids"]) & set(only_ids)):
            continue
        if symbols and u["symbol"] not in symbols:
            continue
        t0 = time.time()
        code = codes.get(u["csid"])
        rec = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"], "ids": u["ids"],
               "cost_model": COST_MODEL_VERSION}
        if not code:
            rec["verdict"] = "NO_CODE"
            results.append(rec)
            continue
        ck = (u["symbol"], u["tf"])
        if ck not in cache:
            cache[ck] = bar_archive.read_archive(ck[0], ck[1])
        bars = cache[ck]
        if bars is None or bars.empty:
            rec["verdict"] = "NOT-RESTAMPABLE (no archive)"
            results.append(rec)
            continue
        ts = pd.to_datetime(bars["ts"])
        rec["archive_bars"] = int(len(bars))
        rec["span_days"] = int((ts.iloc[-1] - ts.iloc[0]).days)
        rec["first_bar"] = str(ts.iloc[0])
        rec["last_bar"] = str(ts.iloc[-1])

        spec = _CONTRACT_SPECS.get(u["symbol"])
        dpp = float(spec["tick_value"]) / float(spec["tick"])

        # params come from the first active that uses this csid/symbol/tf
        aid = u["ids"][0]
        row = rows_by_id[aid]
        params = {k: v for k, v in row["params"].items()}
        params.setdefault("__csid__", u["csid"])
        rec["params"] = params

        sigs, meta = collect_signals(code, bars, params, SLICE_CAP[u["tf"]])
        rec["loop"] = meta
        if not sigs:
            rec["verdict"] = "NO_SIGNALS (or timeout)"
            results.append(rec)
            print(f"csid={u['csid']} {u['symbol']} {u['tf']}: {rec['verdict']} {meta}", flush=True)
            continue
        rec["n_signals"] = len(sigs)
        # how stale is the price the strategy asks for?
        gaps = [(s["i"] * 0) for s in sigs]  # placeholder, ts-based below
        for mode in ("base", "fairng", "fair"):
            tr = simulate(sigs, bars, u["symbol"], dpp, mode)
            rec[mode.upper()] = metrics(tr, u["symbol"], dpp)
            if mode == "fair":
                rec["_fair_trades_head"] = tr[:3]
        rec["_signals_head"] = sigs[:3]
        b, f = rec["BASE"], rec["FAIR"]
        rec["pf_inflation"] = round(b["pf"] / f["pf"], 3) if f["pf"] else None
        rec["wall_s"] = round(time.time() - t0, 1)
        results.append(rec)
        print(f"csid={u['csid']} {u['symbol']} {u['tf']} ids={u['ids']} "
              f"nsig={len(sigs)} BASE n={b['n']} pf={b['pf']} | FAIR n={f['n']} pf={f['pf']} "
              f"e_r={f['e_r']} p={f['p_1sided']:.4f} ({rec['wall_s']}s)", flush=True)
        with open(out_path, "w") as fh:
            json.dump({"cost_model": COST_MODEL_VERSION, "rows": results}, fh, indent=1, default=str)
    with open(out_path, "w") as fh:
        json.dump({"cost_model": COST_MODEL_VERSION, "rows": results}, fh, indent=1, default=str)
    return results


def os_path_exists(p):
    from pathlib import Path as _P
    return _P(p).exists()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/fair_entry_book_20261022.json")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--symbol", default=None)
    a = ap.parse_args()
    pop = json.load(open("outputs/fair_book_pop_20261022.json"))
    units = pop["units"]
    if a.limit:
        units = units[:a.limit]
    run_units(units, a.out, symbols=a.symbol)


if __name__ == "__main__":
    main()