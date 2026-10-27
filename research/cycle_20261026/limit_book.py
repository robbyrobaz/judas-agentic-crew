"""LIMIT BOOK: the most charitable possible reading of a legacy backtest.

WHY
---
The honest-fill audit (research/cycle_20261026/pending_candidate_honest.py)
showed that four pending candidates collapse from PF 2.4/2.2/2.0/1.8 (base
book: fill at the price the strategy NAMES) to PF 0.71/0.56/0.86/1.00 under
the production fill (MARKET at the next bar open -- src/broker/ninjatrader.py:887).

A fair critic could say: "you measured the wrong thing -- a real trader
would have placed a LIMIT order at the named level (the FVG midpoint, the
40% retrace) and only been filled if price came back to it."  That is the
charitable reading, and it is the one that decides whether those four
candidates deserve to be promoted.

THIS FILE implements that charitable book:
  * a signal arms a LIMIT order at the NAMED price (s_entry);
  * the order is filled only if a later bar actually TRADES THROUGH that
    price (long: low <= limit; short: high >= limit);
  * if the market never returns within `limit_bars`, the signal EXPIRES and
    no trade is taken (the house backtest assumed the fill always happened);
  * once filled, the trade uses the strategy's own stop/target;
  * if the fill bar also touches the stop, the STOP wins (conservative);
  * no new signal while a limit is pending or a trade is open.

If the LIMIT book is also <= 1.0, the legacy PF was not a fill artefact at
all -- the edge simply does not exist at any achievable price.

Read-only w.r.t. the registry and the account.
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import load_bars, metrics  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402
from src.tools.cost_model import apply_costs_to_trade  # noqa: E402

DB = ROOT / "judas_crew.db"
TAIL = 700
LIMIT_BARS = 10

JOBS = [
    ("#7378 MNQ ifvg_htf_bias_5m_nogate", 282, "MNQ", "5m", 45, {
        "fvg_expiry": 20, "htf_ema_period": 60, "lookback": 20,
        "min_gap_factor": 0.20, "rr": 2.0, "zone_buffer": 0.15}),
    ("#7379 MGC ifvg_htf_bias_5m_nogate", 282, "MGC", "5m", 45, {
        "fvg_expiry": 20, "htf_ema_period": 60, "lookback": 20,
        "min_gap_factor": 0.20, "rr": 2.0, "zone_buffer": 0.15}),
    ("#7380 MGC judas_continuation_5m", 273, "MGC", "5m", 45, {
        "atr_period": 14, "body_ratio_min": 0.70, "disp_atr_mult": 1.3,
        "pullback_pct": 0.40, "sweep_lookback": 5, "target_r": 1.5,
        "stop_buffer_atr": 0.10}),
    ("#7381 MNQ judas_continuation_5m", 273, "MNQ", "5m", 45, {
        "atr_period": 14, "body_ratio_min": 0.70, "disp_atr_mult": 1.3,
        "pullback_pct": 0.40, "sweep_lookback": 5, "target_r": 1.5,
        "stop_buffer_atr": 0.10}),
    ("#7382 MGC sweep_cont_market_entry_15m (CONTROL)", 439, "MGC", "15m", 120, {
        "lookback": 12, "min_sweep_atr": 0.30, "disp_k": 1.0, "rr": 1.5,
        "confirm_bars": 6, "body_ratio_min": 0.55, "stop_buf_atr": 0.15,
        "atr_period": 14}),
]


def _code(csid: int) -> str:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT code FROM custom_strategies WHERE id=?", (csid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"csid {csid} not found")
    return row[0]


def run_limit_book(code: str, bars, params: dict, symbol: str,
                   qty: int = 1, limit_bars: int = LIMIT_BARS):
    """Replay with a LIMIT entry at the named price (charitable book)."""
    fn, meta = _compile_and_load(code)
    if fn is None:
        return [], {"load_error": meta.get("error")}
    p = dict(params or {})
    sym = str(symbol).upper()
    spec = _CONTRACT_SPECS.get(sym) or {}
    dpp = float(spec.get("tick_value", 1.0)) / float(spec.get("tick", 1.0))

    n = len(bars)
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)

    trades = []
    pending = None      # armed limit order
    open_trade = None
    n_signals = n_expired = n_filled = 0

    for i in range(2, n):
        # ---- manage an open trade -------------------------------------
        if open_trade is not None:
            d = open_trade["direction"]
            if d == "long":
                stop_hit = L[i] <= open_trade["stop"]
                tgt_hit = H[i] >= open_trade["target"]
            else:
                stop_hit = H[i] >= open_trade["stop"]
                tgt_hit = L[i] <= open_trade["target"]
            if stop_hit or tgt_hit:
                px = open_trade["stop"] if stop_hit else open_trade["target"]
                e = open_trade["entry"]
                gross = (px - e) if d == "long" else (e - px)
                pnl = apply_costs_to_trade(
                    symbol=sym, gross_pnl=gross * dpp * qty,
                    exit_reason="target" if tgt_hit and not stop_hit else "stop",
                    qty=qty)
                risk = abs(e - open_trade["stop"]) or 1.0
                trades.append({"entry_bar": open_trade["entry_bar"],
                               "exit_bar": i, "direction": d, "entry": e,
                               "px": px, "pnl": float(pnl),
                               "reason": "target" if tgt_hit and not stop_hit else "stop",
                               "r": float(gross / risk),
                               "bars_held": i - open_trade["entry_bar"]})
                open_trade = None
            continue

        # ---- manage a pending limit order ------------------------------
        if pending is not None:
            d = pending["direction"]
            e = pending["entry"]
            touched = (L[i] <= e) if d == "long" else (H[i] >= e)
            if touched:
                n_filled += 1
                p_stop, p_target = pending["stop"], pending["target"]
                pending = None
                # conservative: if the SAME bar also hits the stop, the stop wins
                stop_hit = (L[i] <= p_stop) if d == "long" else (H[i] >= p_stop)
                tgt_hit = (H[i] >= p_target) if d == "long" else (L[i] <= p_target)
                if stop_hit:
                    gross = ((p_stop - e) if d == "long" else (e - p_stop))
                    pnl = apply_costs_to_trade(
                        symbol=sym, gross_pnl=gross * dpp * qty,
                        exit_reason="stop", qty=qty)
                    trades.append({"entry_bar": i, "exit_bar": i,
                                   "direction": d, "entry": e,
                                   "px": p_stop, "pnl": float(pnl), "reason": "stop",
                                   "r": -1.0, "bars_held": 0})
                    continue
                if tgt_hit:
                    gross = ((p_target - e) if d == "long" else (e - p_target))
                    pnl = apply_costs_to_trade(
                        symbol=sym, gross_pnl=gross * dpp * qty,
                        exit_reason="target", qty=qty)
                    trades.append({"entry_bar": i, "exit_bar": i,
                                   "direction": d, "entry": e,
                                   "px": p_target, "pnl": float(pnl), "reason": "target",
                                   "r": float(gross / (abs(e - p_stop) or 1.0)),
                                   "bars_held": 0})
                    continue
                open_trade = {"direction": d, "entry": e,
                              "stop": p_stop, "target": p_target,
                              "entry_bar": i}
                continue
            if i > pending["expiry"]:
                n_expired += 1
                pending = None
            else:
                continue

        # ---- look for a new signal --------------------------------------
        lo = max(0, i - TAIL + 1)
        try:
            sig = fn(bars.iloc[lo:i + 1], p)
        except Exception:  # noqa: BLE001
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
        sgn = 1.0 if d == "long" else -1.0
        if sgn * (s_stop - s_entry) >= 0 or sgn * (s_target - s_entry) <= 0:
            continue
        n_signals += 1
        pending = {"direction": d, "entry": s_entry, "stop": s_stop,
                   "target": s_target, "expiry": i + limit_bars}

    return trades, {"n_signals": n_signals, "n_filled": n_filled,
                    "n_expired": n_expired, "n_open_at_end": 1 if open_trade else 0,
                    "limit_bars": limit_bars, "symbol": sym, "n_bars": n}


def _job(args):
    label, csid, symbol, tf, days, params = args
    t0 = time.time()
    bars = load_bars(symbol, tf, days=days)
    if bars is None or len(bars) < 200:
        return {"label": label, "error": "no bars"}
    trades, meta = run_limit_book(_code(csid), bars, params, symbol)
    m = metrics(trades)
    m["fill_rate_pct"] = round(100.0 * meta["n_filled"] / meta["n_signals"], 1) \
        if meta["n_signals"] else 0.0
    return {"label": label, "csid": csid, "symbol": symbol, "tf": tf,
            "days": days, "limit": m, "meta": meta,
            "wall_s": round(time.time() - t0, 1)}


def main() -> int:
    out = ROOT / "outputs" / "pending_candidate_limit_20261026.json"
    rows = []
    with ProcessPoolExecutor(max_workers=5) as ex:
        for r in ex.map(_job, JOBS):
            rows.append(r)
            if r.get("error"):
                print(f"{r['label']:<52s} ERROR {r['error']}", flush=True)
                continue
            lm, meta = r["limit"], r["meta"]
            print(f"\n{r['label']}  [{r['symbol']} {r['tf']} {r['days']}d] "
                  f"({r['wall_s']}s)", flush=True)
            print(f"   LIMIT book  n={lm['n']:5d} pf={lm['pf']:6.3f} "
                  f"WR={lm['wr_pct']:5.1f}% E[R]={lm['e_r']:+.3f} "
                  f"${lm['total_usd']:9.0f} dd=${lm['max_dd']:8.0f}", flush=True)
            print(f"   signals={meta['n_signals']} filled={meta['n_filled']} "
                  f"expired(unfilled)={meta['n_expired']} "
                  f"fill_rate={lm['fill_rate_pct']}%", flush=True)
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
