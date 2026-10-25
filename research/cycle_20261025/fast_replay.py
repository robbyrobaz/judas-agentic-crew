"""FAST, FAITHFUL REPLAY HARNESS -- fair/live fill semantics, 2026-10-25 cycle.

WHY
---
Every custom family in the book authors its entry as a STRUCTURAL price
(fvg_mid, ob_mid, mother_h, o[k+1] with k many bars back, h - pullback*rng).
The house backtester fills at that price anyway, so the reported PF is a FILL
ARTEFACT (findings #9126 / #9133).  The live path submits a MARKET order and
re-anchors the bracket to the fill (finding #9134), so the honest number is:

    BASE  entry = sig["entry"]                    (what the house backtester does)
    FAIR  entry = close[decision bar]             (decision-time price)
    LIVE  entry = open[bar AFTER the decision bar] (production semantics)

    exits are GAP-AWARE in fair/live: a bar that gaps through the stop fills
    at the open, not at the stop price.

THIS FILE mirrors research/fair_entry_restamp_book2_20261022.run_variant
(the reference implementation) but evaluates a BOUNDED TAIL WINDOW instead of
the whole growing frame, which turns the O(n^2) replay into O(n*w).  Every
strategy screened here looks back at most 250 bars, so w=700 is semantically
identical to the full prefix.

The one semantic difference the tail window introduces is MODULE-LEVEL STATE:
`_S['last_processed_idx']` guards against re-evaluating the SAME bar.  The
engine never does that (it calls evaluate() once per bar with a fresh index),
so the guard is a no-op there -- but in a tail replay the index is constant
and the guard would suppress every signal after the first.  `_patch_state_guard`
therefore neutralises that guard.  The neutralisation is a no-op under the
engine, and BASE mode is validated against the real engine before any LIVE
verdict is trusted (see validate_against_engine).

READ-ONLY w.r.t. the registry and the account.
"""
from __future__ import annotations

import json
import math
import re
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
TAIL = 700


# ------------------------------------------------------------------ data ----
def load_bars(symbol: str, tf: str, days: int = 120):
    df = bar_archive.read_archive(symbol.upper(), tf)
    if df is None or len(df) == 0:
        return None
    per = BARS_PER_DAY.get(tf, 24)
    if days and len(df) > per * days:
        df = df.tail(per * days).reset_index(drop=True)
    return df.reset_index(drop=True)


# ------------------------------------------------------------------ code ----
_STATE_GUARD = re.compile(
    r"if\s+(?:cur_i|n\s*-\s*1|_i)\s*==\s*_S\[[^\]]+\]\s*:\s*return\s+None",
)


def _patch_state_guard(code: str) -> str:
    """Neutralise the per-bar dedup guard (a no-op under the engine)."""
    return _STATE_GUARD.sub("if False: return None", code)


# ---------------------------------------------------------------- replay ----
def replay(code: str, bars, params: dict, symbol: str, mode: str = "live",
           tail: int = TAIL, qty: int = 1):
    """Replay `bars` and return (trades, meta) under the requested fill mode."""
    fn, meta = _compile_and_load(code)
    if fn is None:
        return None, {"load_error": meta.get("error")}
    p = dict(params or {})
    sym = str(symbol).upper()
    spec = _CONTRACT_SPECS.get(sym) or {}
    dpp = float(spec.get("tick_value", 1.0)) / float(spec.get("tick", 1.0))

    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    trades = []
    open_t = None
    n_signals = 0
    eval_errors = 0
    outside = at_close = 0
    offs = []
    t0 = time.time()
    timed_out = False
    budget_s = 420.0

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
            if not (stop_hit or tgt_hit):
                continue
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
            pnl = apply_costs_to_trade(symbol=sym, gross_pnl=gross * dpp * qty,
                                       exit_reason="target" if "target" in reason else "stop",
                                       qty=qty)
            risk = abs(e - open_t["stop"]) or 1.0
            trades.append({"entry_bar": open_t["entry_bar"], "exit_bar": i,
                           "direction": d, "entry": round(float(e), 8),
                           "px": round(float(px), 8), "reason": reason,
                           "pnl": float(pnl), "r": float(gross / risk),
                           "bars_held": i - open_t["entry_bar"]})
            open_t = None
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
        n_signals += 1
        rng = max(H[i] - L[i], 1e-12)
        outside += 0 if (L[i] - 1e-9 <= s_entry <= H[i] + 1e-9) else 1
        at_close += 1 if abs(s_entry - C[i]) <= 1e-9 else 0
        offs.append(abs(s_entry - C[i]) / rng)

        if mode == "base":
            entry = s_entry
            stop, target = s_stop, s_target
        elif mode == "live":
            if i + 1 >= n:
                continue
            entry = O[i + 1]
            sign = 1.0 if d == "long" else -1.0
            stop = entry - sign * sd
            target = entry + sign * td
        else:  # fair
            entry = C[i]
            sign = 1.0 if d == "long" else -1.0
            stop = entry - sign * sd
            target = entry + sign * td
        open_t = {"direction": d, "entry": entry, "stop": stop, "target": target,
                  "entry_bar": i + 1}

    dt = time.time() - t0
    meta2 = {
        "bars_replayed": min(i + 1, n), "n_bars": n, "n_signals": n_signals,
        "eval_errors": eval_errors, "timed_out": timed_out,
        "loop_s": round(dt, 1), "mode": mode, "symbol": sym,
        "cost_model": COST_MODEL_VERSION,
    }
    if n_signals:
        meta2["pct_entry_outside_decision_bar"] = round(100.0 * outside / n_signals, 1)
        meta2["pct_entry_is_decision_close"] = round(100.0 * at_close / n_signals, 1)
        meta2["entry_offset_x_bar_range_median"] = round(float(np.median(offs)), 3)
        meta2["entry_offset_x_bar_range_p90"] = round(float(np.percentile(offs, 90)), 3)
    return trades, meta2


def metrics(trades):
    if not trades:
        return {"n": 0, "pf": 0.0, "wr_pct": 0.0, "usd_per_trade": 0.0,
                "e_r": 0.0, "total_usd": 0.0, "max_dd": 0.0,
                "avg_bars_held": 0.0, "n_stop_gap": 0, "n_target_gap": 0}
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
            "usd_per_trade": round(sum(pnls) / n, 2),
            "e_r": round(float(rs.mean()), 3), "total_usd": round(sum(pnls), 2),
            "max_dd": round(dd, 2),
            "avg_bars_held": round(sum(t["bars_held"] for t in trades) / n, 1),
            "n_stop_gap": sum(1 for t in trades if t["reason"] == "stop_gap"),
            "n_target_gap": sum(1 for t in trades if t["reason"] == "target_gap")}
