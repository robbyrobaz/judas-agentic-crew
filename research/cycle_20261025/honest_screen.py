"""HONEST-FILL SCREEN (2026-10-25 cycle).

WHY THIS FILE
-------------
Finding #9126 / #9133 / #9139 established that the house backtester
(`run_custom_backtest_on_bars`) FILLS AT THE PRICE THE STRATEGY NAMES.  Every
family in the book names a STRUCTURAL price (FVG midpoint, OB midpoint, the
sweep extreme, `mother_h`, ...).  That price belongs to a bar that had already
closed, so the fill is retroactive: the move from the named price to the
decision-bar close is never risked.  The stamp says nothing about this unless
the strategy volunteers a `ts` key -- and only 72 of 437 registered custom
strategies do (so 83% of the book gets `backtest_fill_valid=true` with NO
stale measurement at all).

WHAT THIS FILE DOES
--------------------
One evaluate() pass, TWO books:

    base : entry = sig["entry"]                       (what the house backtester does)
    live : entry = open of the bar AFTER the decision bar, brackets re-anchored
           to the fill, gap-aware exits                (production semantics, #9134)

Same signals, same stops/targets, same bars, same cost model.  The gap between
the two books IS the retro-fill artefact, measured per config.

`live` is the number that matters: it is the only fill the broker can give us
(the live path submits a MARKET order -- finding #9134 says there is no LIMIT
entry path anywhere in this repo).

READ-ONLY w.r.t. the registry and the account.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src import bar_archive  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import COST_MODEL_VERSION, apply_costs_to_trade  # noqa: E402

BARS_PER_DAY = {"1h": 24, "15m": 96, "5m": 288}
TAIL = 700  # strategies here look back at most ~250 bars


# ------------------------------------------------------------------ data ----
def load_bars(symbol: str, tf: str, days: int = 120):
    df = bar_archive.read_archive(str(symbol).upper(), tf)
    if df is None or len(df) == 0:
        return None
    per = BARS_PER_DAY.get(tf, 24)
    if days and len(df) > per * days:
        df = df.tail(per * days).reset_index(drop=True)
    return df.reset_index(drop=True)


# ----------------------------------------------------------------- replay ---
def run_books(code: str, bars, params: dict, symbol: str, qty: int = 1,
              tail: int = TAIL, budget_s: float = 300.0):
    """Evaluate one config and return (base_trades, live_trades, meta)."""
    fn, meta = _compile_and_load(code)
    if fn is None:
        return [], [], {"load_error": meta.get("error")}
    p = dict(params or {})
    sym = str(symbol).upper()
    spec = _CONTRACT_SPECS.get(sym) or {}
    dpp = float(spec.get("tick_value", 1.0)) / float(spec.get("tick", 1.0))

    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    base_t, live_t, fair_t = [], [], []
    open_base = open_live = open_fair = None
    n_signals = 0
    eval_errors = 0
    outside = 0
    entry_off = []
    t0 = time.time()
    timed_out = False
    i = 2

    for i in range(2, n):
        if time.time() - t0 > budget_s:
            timed_out = True
            break

        # ---- manage open trades (all books) on this bar ----
        for mode, ot in (("base", open_base), ("live", open_live),
                         ("fair", open_fair)):
            if ot is None:
                continue
            d = ot["direction"]
            o, h, l = O[i], H[i], L[i]
            if d == "long":
                stop_hit = l <= ot["stop"]
                tgt_hit = h >= ot["target"]
            else:
                stop_hit = h >= ot["stop"]
                tgt_hit = l <= ot["target"]
            if not (stop_hit or tgt_hit):
                continue
            if mode in ("live", "fair"):  # gap-aware
                if d == "long":
                    if stop_hit and o <= ot["stop"]:
                        px, reason = o, "stop_gap"
                    elif stop_hit:
                        px, reason = ot["stop"], "stop"
                    elif o >= ot["target"]:
                        px, reason = o, "target_gap"
                    else:
                        px, reason = ot["target"], "target"
                else:
                    if stop_hit and o >= ot["stop"]:
                        px, reason = o, "stop_gap"
                    elif stop_hit:
                        px, reason = ot["stop"], "stop"
                    elif o <= ot["target"]:
                        px, reason = o, "target_gap"
                    else:
                        px, reason = ot["target"], "target"
            else:  # base == house engine semantics (fill at the named level)
                if stop_hit:
                    px, reason = ot["stop"], "stop"
                else:
                    px, reason = ot["target"], "target"
            e = ot["entry"]
            gross = (px - e) if d == "long" else (e - px)
            pnl = apply_costs_to_trade(symbol=sym, gross_pnl=gross * dpp * qty,
                                       exit_reason="target" if "target" in reason else "stop",
                                       qty=qty)
            risk = abs(e - ot["stop"]) or 1.0
            rec = {"entry_bar": ot["entry_bar"], "exit_bar": i, "direction": d,
                   "entry": round(float(e), 8), "px": round(float(px), 8),
                   "reason": reason, "pnl": float(pnl), "r": float(gross / risk),
                   "bars_held": i - ot["entry_bar"]}
            if mode == "base":
                base_t.append(rec)
                open_base = None
            elif mode == "live":
                live_t.append(rec)
                open_live = None
            else:
                fair_t.append(rec)
                open_fair = None

        if open_base is not None and open_live is not None and open_fair is not None:
            continue

        # ---- look for a new signal ----
        if open_base is not None or open_live is not None or open_fair is not None:
            continue

        lo = max(0, i - tail + 1)
        try:
            sig = fn(bars.iloc[lo:i + 1], p)
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
        if not (math.isfinite(s_entry) and math.isfinite(s_stop)
                and math.isfinite(s_target)):
            continue
        sd = abs(s_entry - s_stop)
        td = abs(s_target - s_entry)
        if sd <= 0 or td <= 0:
            continue
        # GEOMETRY GUARD: the stop must sit on the losing side of the entry.
        # Without this, a strategy whose stop is ABOVE the entry for a long
        # (or BELOW it for a short) is "hit" on the very next bar and the
        # BASE book books it as a +1R WIN (px - entry > 0).  That is an
        # accounting artefact, not an edge: it inflated fvg_touch to a fake
        # base PF of 3.64 (LIVE 1.09).  The house engine books the same trade
        # as a loss, so the two books disagree -- the honest book must skip
        # it outright, and so must any strategy we promote.
        sgn = 1.0 if d == "long" else -1.0
        if sgn * (s_stop - s_entry) <= 0 or sgn * (s_target - s_entry) <= 0:
            continue
        n_signals += 1
        rng = max(H[i] - L[i], 1e-12)
        outside += 0 if (L[i] - 1e-9 <= s_entry <= H[i] + 1e-9) else 1
        entry_off.append(abs(s_entry - C[i]) / rng)

        if i + 1 >= n:
            continue
        # base book: the house engine's own fill
        open_base = {"direction": d, "entry": s_entry, "stop": s_stop,
                     "target": s_target, "entry_bar": i + 1}
        # live book: MARKET at the next bar's open, brackets re-anchored
        entry = O[i + 1]
        sign = 1.0 if d == "long" else -1.0
        open_live = {"direction": d, "entry": entry, "stop": entry - sign * sd,
                     "target": entry + sign * td, "entry_bar": i + 1}
        # fair book: fill at the decision bar's close (market-on-close)
        entry_f = C[i]
        open_fair = {"direction": d, "entry": entry_f, "stop": entry_f - sign * sd,
                     "target": entry_f + sign * td, "entry_bar": i + 1}

    meta2 = {
        "bars": min(i + 1, n), "n_bars": n, "n_signals": n_signals,
        "eval_errors": eval_errors, "timed_out": timed_out,
        "loop_s": round(time.time() - t0, 1), "symbol": sym,
        "cost_model": COST_MODEL_VERSION,
        "first_bar": str(bars["ts"].iloc[0]), "last_bar": str(bars["ts"].iloc[-1]),
    }
    if n_signals:
        meta2["pct_entry_outside_decision_bar"] = round(100.0 * outside / n_signals, 1)
        meta2["entry_offset_x_bar_range_median"] = round(float(np.median(entry_off)), 3)
    return base_t, live_t, fair_t, meta2


# ---------------------------------------------------------------- metrics ---
def metrics(trades):
    if not trades:
        return {"n": 0, "pf": 0.0, "wr_pct": 0.0, "e_r": 0.0, "total_usd": 0.0,
                "max_dd": 0.0, "usd_per_trade": 0.0, "avg_bars_held": 0.0,
                "n_stop_gap": 0}
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    pf = (gw / gl) if gl > 0 else 999.0
    rs = np.array([t["r"] for t in trades], float)
    run = peak = dd = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {"n": n, "pf": round(pf, 3),
            "wr_pct": round(100.0 * sum(1 for x in pnls if x > 0) / n, 1),
            "e_r": round(float(rs.mean()), 3),
            "total_usd": round(sum(pnls), 2),
            "max_dd": round(dd, 2),
            "usd_per_trade": round(sum(pnls) / n, 2),
            "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1),
            "n_stop_gap": sum(1 for t in trades if t["reason"] == "stop_gap")}


def screen(code: str, symbol: str, tf: str, params: dict, days: int = 120,
           qty: int = 1):
    bars = load_bars(symbol, tf, days=days)
    if bars is None or len(bars) < 200:
        return {"error": "no bars", "symbol": symbol, "tf": tf}
    b, lv, fa, meta = run_books(code, bars, params, symbol, qty=qty)
    if meta.get("eval_errors"):
        meta["EVAL_ERRORS"] = meta["eval_errors"]
    return {"symbol": symbol, "tf": tf, "days": days, "params": params,
            "base": metrics(b), "live": metrics(lv), "fair": metrics(fa),
            "meta": meta}


if __name__ == "__main__":
    print(json.dumps(screen(*sys.argv[1:]) if False else {}, indent=2))
