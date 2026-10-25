"""FAIR ENTRY RESTAMP HARNESS v2 -- task #2869.

v2 fixes the two fidelity defects in v1 (found by the port validation):

  1. A capped prefix slice breaks module-level state strategies
     (`_S['last_processed_idx']` saturates -> every later signal is
     suppressed).  v2 replays the FULL prefix, exactly like the engine.
  2. The engine does NOT call evaluate() while a trade is open.  Collecting
     signals unconditionally in a separate pass changes the stream for any
     stateful strategy.  v2 interleaves state and evaluation inside the loop,
     mirroring the engine line for line.

The ONLY difference between variants is the fill:

  BASE    entry = sig["entry"]; exits filled AT the stop/target price.
          This is a literal clone of `run_custom_backtest_on_bars`.
  FAIRNG  entry = close[decision bar]; authored stop/target distances
          preserved; exits still filled AT stop/target (no gap logic).
  FAIR    entry = close[decision bar]; same distances; gap-aware exits
          (a stop the bar gapped through fills at the open).
  LIVE    entry = open[bar AFTER the decision bar]; same distances; gap-aware
          exits.  This is the production semantics: the live path submits a
          MARKET order at decision time and the fill lands on the next bar's
          open (finding #9134: "the live path is a MARKET order with
          fill-anchored brackets").  It exists so the verdict does not
          depend on the choice of entry model -- if BASE/FAIR/LIVE all say the
          same thing, the conclusion is about the strategy, not the model.

READ-ONLY w.r.t. registry/account.
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

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade  # noqa: E402

UNIT_BUDGET_S = 420.0
PER_CALL_WARN_S = 2.0


# ---------------------------------------------------------------- stats ----
def _betacf(a, b, x):
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
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 3e-7:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lb = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
          + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lb) * _betacf(b, a, 1.0 - x) / b


def t_sf_1samp(t: float, df: int) -> float:
    if df <= 0 or not math.isfinite(t) or t <= 0:
        return 1.0 if not math.isfinite(t) or t <= 0 else 1.0
    x = df / (df + t * t)
    return 0.5 * _betai(0.5 * df, 0.5, x)


# ----------------------------------------------------------------- loop ----
def run_variant(code, bars, params, symbol, dpp, mode, budget_s=UNIT_BUDGET_S,
                want_stale=False):
    fn, meta = _compile_and_load(code)
    if fn is None:
        return None, {"load_error": meta.get("error")}
    p = dict(params)
    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    TS = bars["ts"].to_numpy()
    ts_index = {}
    for k in range(n):
        ts_index[TS[k]] = k

    trades = []
    open_t = None
    n_signals = 0
    eval_errors = 0
    stale_bars = []
    outside, at_close, offs = [], [], []
    t0 = time.time()
    timed_out = False
    for i in range(2, n):
        if time.time() - t0 > budget_s:
            timed_out = True
            break
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
                if mode in ("fair", "live"):
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
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross * dpp,
                                           exit_reason="target" if "target" in reason else "stop")
                risk = abs(e - open_t["stop"]) or 1.0
                trades.append({"entry_bar": open_t["entry_bar"], "exit_bar": i,
                               "direction": d, "entry": round(e, 6), "px": round(px, 6),
                               "reason": reason, "pnl": float(pnl),
                               "r": float(gross / risk), "bars_held": i - open_t["entry_bar"]})
                open_t = None
                continue
            continue  # in trade: engine does NOT evaluate this bar
        try:
            sig = fn(bars.iloc[:i + 1], p)
        except Exception:  # noqa: BLE001
            eval_errors += 1
            continue
        if not isinstance(sig, dict):
            continue
        d = str(sig.get("direction", "")).lower()
        if d not in ("long", "short"):
            continue
        try:
            s_entry = float(sig["entry"])
            s_stop = float(sig["stop"])
            s_target = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(s_entry) and math.isfinite(s_stop) and math.isfinite(s_target)):
            continue
        sd = abs(s_entry - s_stop)
        td = abs(s_target - s_entry)
        if sd <= 0 or td <= 0:
            continue
        n_signals += 1
        if want_stale:
            # UNIVERSAL staleness test -- no strategy cooperation needed.
            # A price the market offered DURING the decision bar lies inside
            # [low, high] of that bar.  Anything outside it was never
            # available to a decision-time order.
            lo_i, hi_i, c_i = L[i], H[i], C[i]
            rng = max(hi_i - lo_i, 1e-9)
            outside.append(0 if lo_i - 1e-9 <= s_entry <= hi_i + 1e-9 else 1)
            at_close.append(1 if abs(s_entry - c_i) <= 1e-6 else 0)
            offs.append(abs(s_entry - c_i) / rng)
            sig_ts = sig.get("ts")
            j = ts_index.get(sig_ts)
            if j is None:
                try:
                    j = ts_index.get(np.datetime64(sig_ts))
                except Exception:  # noqa: BLE001
                    j = None
            if j is None:
                for k in range(max(0, i - 200), i + 1):
                    if str(TS[k]) == str(sig_ts):
                        j = k
                        break
            stale_bars.append((i - j) if j is not None else None)
        if mode == "base":
            entry = s_entry
        elif mode == "live":
            # Decision is taken on bar i; a live market order fills on bar
            # i+1's open.  If there is no next bar there is no fill.
            if i + 1 >= n:
                continue
            entry = O[i + 1]
        else:
            entry = C[i]
        sign = 1.0 if d == "long" else -1.0
        open_t = {"direction": d, "entry": entry,
                  "stop": entry - sign * sd, "target": entry + sign * td,
                  "entry_bar": i + 1}
    dt = time.time() - t0
    if outside:
        meta2_pct = {"pct_entry_outside_decision_bar": round(100.0 * sum(outside) / len(outside), 1),
                     "pct_entry_is_decision_close": round(100.0 * sum(at_close) / len(at_close), 1),
                     "entry_offset_x_bar_range_median": round(float(np.median(offs)), 3),
                     "entry_offset_x_bar_range_p90": round(float(np.percentile(offs, 90)), 3)}
    else:
        meta2_pct = {}
    sb = [x for x in stale_bars if x is not None]
    meta2 = {"bars_replayed": min(i + 1, n), "n_bars": n, "n_signals": n_signals,
             "eval_errors": eval_errors, "timed_out": timed_out, "loop_s": round(dt, 1),
             "ms_per_call": round(1000 * dt / max(1, n - 2), 3)}
    meta2.update(meta2_pct)
    if sb:
        arr = np.array(sb, float)
        meta2["stale_bars_median"] = float(np.median(arr))
        meta2["stale_bars_p90"] = float(np.percentile(arr, 90))
        meta2["stale_bars_max"] = float(arr.max())
        meta2["pct_stale_gt_0"] = round(100.0 * float((arr > 0).mean()), 1)
        meta2["pct_stale_gt_2"] = round(100.0 * float((arr > 2).mean()), 1)
        meta2["pct_unmeasured_ts"] = round(100.0 * sum(1 for x in stale_bars if x is None)
                                          / len(stale_bars), 1)
    meta2.update(meta)
    return trades, meta2


def metrics(trades):
    if not trades:
        return {"n": 0, "pf": 0.0, "wr_pct": 0.0, "usd_per_trade": 0.0, "e_r": 0.0,
                "total_usd": 0.0, "p_1sided": 1.0, "max_dd": 0.0, "avg_bars_held": 0.0,
                "n_stop_gap": 0}
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
            "total_usd": round(sum(pnls), 2), "p_1sided": round(p, 5), "t_stat": round(tstat, 3),
            "max_dd": round(dd, 2), "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1),
            "n_stop_gap": sum(1 for t in trades if t["reason"] == "stop_gap"),
            "n_target_gap": sum(1 for t in trades if t["reason"] == "target_gap")}


# ----------------------------------------------------------------- main ----
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/fair_entry_book_20261022.json")
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--chunks", type=int, default=1)
    ap.add_argument("--budget", type=float, default=UNIT_BUDGET_S)
    ap.add_argument("--modes", default="base,fair")
    a = ap.parse_args()

    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {i: c for i, c in con.execute("SELECT id, code FROM custom_strategies")}
    con.close()
    pop = json.load(open("outputs/fair_book_pop_20261022.json"))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    units = pop["units"]
    modes = a.modes.split(",")

    outpath = Path(a.out)
    if a.chunks > 1:
        outpath = outpath.with_suffix(f".part{a.chunk}")
    results = []
    if outpath.exists():
        try:
            results = json.load(open(outpath))["rows"]
        except Exception:  # noqa: BLE001
            results = []
    done = {(r["csid"], r["symbol"], r["tf"]) for r in results}

    cache = {}
    for u in units:
        key = (u["csid"], u["symbol"], u["tf"])
        if key in done:
            continue
        if a.chunks > 1 and units.index(u) % a.chunks != a.chunk:
            continue
        t0 = time.time()
        rec = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"], "ids": u["ids"],
               "cost_model": COST_MODEL_VERSION}
        code = codes.get(u["csid"])
        if not code:
            rec["verdict"] = "NO_CODE"
            results.append(rec)
            continue
        ck = (u["symbol"], u["tf"])
        if ck not in cache:
            cache[ck] = bar_archive.read_archive(ck[0], ck[1])
        bars = cache[ck]
        if bars is None or len(bars) == 0:
            rec["verdict"] = "NOT-RESTAMPABLE (no archive)"
            results.append(rec)
            continue
        ts = bars["ts"]
        rec["archive_bars"] = int(len(bars))
        rec["span_days"] = int((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400)
        rec["first_bar"] = str(ts.iloc[0])
        rec["last_bar"] = str(ts.iloc[-1])
        spec = _CONTRACT_SPECS.get(u["symbol"])
        dpp = float(spec["tick_value"]) / float(spec["tick"])
        params = dict(rows_by_id[u["ids"][0]]["params"])
        params.setdefault("__csid__", u["csid"])
        rec["params"] = {k: v for k, v in params.items() if k != "__csid__"}

        for mode in modes:
            tr, meta = run_variant(code, bars, params, u["symbol"], dpp, mode,
                                   budget_s=a.budget, want_stale=(mode == modes[0]))
            rec[mode.upper()] = metrics(tr or [])
            rec[f"{mode.upper()}_meta"] = meta
        b = rec.get("BASE", {})
        f = rec.get("FAIR", {})
        rec["pf_inflation"] = round(b.get("pf", 0) / f["pf"], 3) if f.get("pf") else None
        rec["usd_ratio_BASE_over_FAIR"] = (round(b.get("usd_per_trade", 0) / f["usd_per_trade"], 2)
                                           if f.get("usd_per_trade") else None)
        rec["wall_s"] = round(time.time() - t0, 1)
        results.append(rec)
        print(f"[{u['symbol']} {u['tf']} csid={u['csid']} ids={u['ids']}] "
              f"BASE n={b.get('n')} pf={b.get('pf')} wr={b.get('wr_pct')} "
              f"| FAIR n={f.get('n')} pf={f.get('pf')} wr={f.get('wr_pct')} "
              f"e_r={f.get('e_r')} p={f.get('p_1sided')} ({rec['wall_s']}s)", flush=True)
        with open(outpath, "w") as fh:
            json.dump({"cost_model": COST_MODEL_VERSION, "rows": results}, fh, indent=1, default=str)
    with open(outpath, "w") as fh:
        json.dump({"cost_model": COST_MODEL_VERSION, "rows": results}, fh, indent=1, default=str)
    print("DONE", outpath, len(results), flush=True)


if __name__ == "__main__":
    main()