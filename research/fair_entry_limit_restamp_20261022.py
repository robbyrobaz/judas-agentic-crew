"""LIMIT-FAIR RESTAMP -- task #2869, third measurement class.

The FAIR variant (fill at the decision bar's close) is the conservative
market-order restamp.  But a large share of this book RETURNS A LIMIT/STOP
PRICE rather than a market price -- e.g. `silver_bullet_pdh_pdl_retest_*`
returns `o[entry_idx]`, the open of a bar that may be many bars back.  Under
the current engine that price is granted unconditionally, which is the
retro-fill defect (9121).

Filling such a strategy at the decision close punishes it for something the
strategy did not do wrong: it asked for a limit order and never got filled.
The honest third measurement is to treat the authored price as a real order:

  * entry >= decision close (long)  -> STOP order: fill when high >= entry
  * entry <  decision close (long)  -> LIMIT order: fill when low  <= entry
  (mirrored for shorts)
  * if price never trades through it within `limit_bars`, THE SIGNAL NEVER
    FILLS and produces no trade at all.
  * once filled, stop/target are the authored distances, gap-aware exits.

`limit_bars` defaults to params.max_retest_bars (the strategies' own declared
patience) and falls back to 12 bars.

This isolates three things the other two modes conflate:
  BASE   = engine as it stands
  FAIR   = "market it at decision time"  (pessimistic for limit authors)
  LIMIT  = "give the order the chance it asked for" (fair to limit authors)
"""
from __future__ import annotations

import argparse
import json
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
from research.fair_entry_restamp_book2_20261022 import metrics, t_sf_1samp  # noqa: E402

UNIT_BUDGET_S = 300.0
DEFAULT_LIMIT_BARS = 12


def run_limit(code, bars, params, symbol, dpp, budget_s=UNIT_BUDGET_S):
    fn, meta = _compile_and_load(code)
    if fn is None:
        return None, {"load_error": meta.get("error")}
    p = dict(params)
    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    lim_bars = int(params.get("max_retest_bars", DEFAULT_LIMIT_BARS) or DEFAULT_LIMIT_BARS)
    lim_bars = max(1, min(lim_bars, 60))

    trades = []
    open_t = None
    pending = None
    n_signals = n_filled = n_expired = 0
    order_kind = {"stop": 0, "limit": 0}
    eval_errors = 0
    fill_delay = []
    t0 = time.time()
    timed_out = False
    for i in range(2, n):
        if time.time() - t0 > budget_s:
            timed_out = True
            break
        # ---- 1. manage an open position -------------------------------------
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
            continue
        # ---- 2. manage a working order --------------------------------------
        if pending is not None:
            d = pending["direction"]
            e = pending["entry"]
            if i > pending["expiry"]:
                n_expired += 1
                pending = None
                continue
            if (pending["kind"] == "limit" and
                    ((d == "long" and L[i] <= e) or (d == "short" and H[i] >= e))) or \
               (pending["kind"] == "stop" and
                    ((d == "long" and H[i] >= e) or (d == "short" and L[i] <= e))):
                open_t = {"direction": d, "entry": e, "stop": pending["stop"],
                          "target": pending["target"], "entry_bar": i}
                n_filled += 1
                fill_delay.append(i - pending["sig_bar"])
                pending = None
                continue
            continue
        # ---- 3. evaluate -----------------------------------------------------
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
        if not all(np.isfinite([s_entry, s_stop, s_target])):
            continue
        sd = abs(s_entry - s_stop)
        td = abs(s_target - s_entry)
        if sd <= 0 or td <= 0:
            continue
        n_signals += 1
        c = C[i]
        sign = 1.0 if d == "long" else -1.0
        if d == "long":
            kind = "limit" if s_entry < c else "stop"
        else:
            kind = "limit" if s_entry > c else "stop"
        order_kind[kind] += 1
        # already touched on the decision bar itself -> filled immediately
        touched_now = ((kind == "limit" and (L[i] <= s_entry if d == "long" else H[i] >= s_entry)) or
                       (kind == "stop" and (H[i] >= s_entry if d == "long" else L[i] <= s_entry)))
        if touched_now:
            open_t = {"direction": d, "entry": s_entry,
                      "stop": s_entry - sign * sd, "target": s_entry + sign * td,
                      "entry_bar": i + 1}
            n_filled += 1
            fill_delay.append(1)
            continue
        pending = {"direction": d, "entry": s_entry, "stop": s_entry - sign * sd,
                   "target": s_entry + sign * td, "kind": kind, "sig_bar": i,
                   "expiry": i + lim_bars}
    dt = time.time() - t0
    m = {"bars_replayed": min(i + 1, n), "n_bars": n, "n_signals": n_signals,
         "n_orders_filled": n_filled, "n_orders_expired_unfilled": n_expired,
         "order_kind_stop": order_kind["stop"], "order_kind_limit": order_kind["limit"],
         "fill_rate_pct": round(100.0 * n_filled / n_signals, 1) if n_signals else 0.0,
         "eval_errors": eval_errors, "timed_out": timed_out, "loop_s": round(dt, 1),
         "limit_bars": lim_bars, "ms_per_call": round(1000 * dt / max(1, n - 2), 3)}
    if fill_delay:
        m["fill_delay_median_bars"] = float(np.median(fill_delay))
    m.update(meta)
    return trades, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="outputs/fair_entry_limit_20261022.json")
    ap.add_argument("--chunk", type=int, default=0)
    ap.add_argument("--chunks", type=int, default=1)
    ap.add_argument("--budget", type=float, default=UNIT_BUDGET_S)
    a = ap.parse_args()

    con = sqlite3.connect("file:judas_crew.db?mode=ro", uri=True)
    codes = {i: c for i, c in con.execute("SELECT id, code FROM custom_strategies")}
    con.close()
    pop = json.load(open("outputs/fair_book_pop_20261022.json"))
    rows_by_id = {r["id"]: r for r in pop["rows"]}
    units = pop["units"]

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
    for idx, u in enumerate(units):
        key = (u["csid"], u["symbol"], u["tf"])
        if key in done:
            continue
        if a.chunks > 1 and idx % a.chunks != a.chunk:
            continue
        t0 = time.time()
        rec = {"csid": u["csid"], "symbol": u["symbol"], "tf": u["tf"], "ids": u["ids"],
               "cost_model": COST_MODEL_VERSION}
        code = codes.get(u["csid"])
        bars = None
        ck = (u["symbol"], u["tf"])
        if ck not in cache:
            cache[ck] = bar_archive.read_archive(ck[0], ck[1])
        bars = cache[ck]
        if not code or bars is None or len(bars) == 0:
            rec["verdict"] = "NOT-RESTAMPABLE"
            results.append(rec)
            continue
        ts = bars["ts"]
        rec["archive_bars"] = int(len(bars))
        rec["span_days"] = int((ts.iloc[-1] - ts.iloc[0]).days / 86400e9)
        spec = _CONTRACT_SPECS[u["symbol"]]
        dpp = float(spec["tick_value"]) / float(spec["tick"])
        params = dict(rows_by_id[u["ids"][0]]["params"])
        params.setdefault("__csid__", u["csid"])
        tr, m = run_limit(code, bars, params, u["symbol"], dpp, budget_s=a.budget)
        rec["LIMIT"] = metrics(tr or [])
        rec["LIMIT_meta"] = m
        rec["wall_s"] = round(time.time() - t0, 1)
        results.append(rec)
        L = rec["LIMIT"]
        print(f"[{u['symbol']} {u['tf']} csid={u['csid']} ids={u['ids']}] "
              f"LIMIT n={L['n']} pf={L['pf']} wr={L['wr_pct']} e_r={L['e_r']} "
              f"p={L['p_1sided']} fill_rate={m.get('fill_rate_pct')}% ({rec['wall_s']}s)", flush=True)
        with open(outpath, "w") as fh:
            json.dump({"rows": results}, fh, indent=1, default=str)
    with open(outpath, "w") as fh:
        json.dump({"rows": results}, fh, indent=1, default=str)
    print("DONE", outpath, len(results), flush=True)


if __name__ == "__main__":
    main()