"""Fast local replay for the v2 (data-hygiene) sweep-MSS-FVG family.

Mirrors src/research/custom_strategy_runtime.run_custom_backtest_on_bars:
  * one trade at a time,
  * fill at the price the signal names (for this family: the decision bar's
    CLOSE -- an executable market fill),
  * stop takes precedence when a bar touches both brackets (adverse-first),
  * costs through src.tools.cost_model v1_realistic_micros, qty=1.

Unlike the house engine it calls the strategy's `decide()` on raw numpy
arrays (no per-bar pandas slicing), so a full 15m pass takes ~1s.

The trades are returned with their exit timestamp so a caller can split
IS / OOS by TIME without replaying twice (no boundary artefact).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

STRAT_PATH = (ROOT / "research" / "cycle_20261028b" / "strategies" /
              "sweep_mss_fvg_liq_target_v2.py")
PER_DAY = {"5m": 288, "15m": 96, "1h": 24}


def load_strategy(path=STRAT_PATH):
    spec = importlib.util.spec_from_file_location("sweep_mss_v2", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_bars(symbol: str, tf: str):
    df = pd.read_parquet(ROOT / f"data/bars_archive/{symbol}_{tf}.parquet")
    return df.sort_values("ts").reset_index(drop=True)


def _arrays(df):
    return (df["high"].to_numpy(float), df["low"].to_numpy(float),
            df["close"].to_numpy(float), df["open"].to_numpy(float),
            df["volume"].to_numpy(float) if "volume" in df.columns else None,
            df["ts"].to_numpy())


def replay(df, params, symbol, mod, qty=1):
    from src.tools.cost_model import apply_costs_to_trade
    from src.portfolio_runtime import _CONTRACT_SPECS
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    H, L, C, O, V, TS = _arrays(df)
    n = len(df)
    p = mod._merged_params(params)
    TAIL = int(mod.TAIL)

    trades = []
    open_t = None
    for i in range(2, n):
        if open_t is not None:
            t = open_t
            if t["direction"] == "long":
                hit_stop = L[i] <= t["stop"]
                hit_tgt = H[i] >= t["target"]
            else:
                hit_stop = H[i] >= t["stop"]
                hit_tgt = L[i] <= t["target"]
            if hit_stop or hit_tgt:
                entry = t["entry"]
                risk = abs(entry - t["stop"]) or 1.0
                won = hit_tgt and not hit_stop
                pts = abs(t["target"] - entry) if won else -abs(entry - t["stop"])
                gross = pts * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason="target" if won else "stop",
                                           qty=qty)
                trades.append({**t, "exit_i": i, "ts": TS[i], "won": bool(won),
                               "pnl": float(pnl),
                               "r": float(pnl / (risk * dpp * qty))})
                open_t = None
            continue
        lo = max(0, i - TAIL + 1)
        sig = mod.decide(H[lo:i + 1], L[lo:i + 1], C[lo:i + 1], O[lo:i + 1],
                         None if V is None else V[lo:i + 1], p)
        if sig is None:
            continue
        direction, entry, stop, target = sig
        open_t = {"direction": direction, "entry": entry, "stop": stop,
                  "target": target, "entry_i": i, "entry_ts": TS[i]}
    return trades


def agg(trades):
    if not trades:
        return {"n": 0, "w": 0, "l": 0, "pf": 0.0, "pnl": 0.0, "e_r": 0.0,
                "maxdd": 0.0, "wr": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for x in pnls if x > 0)
    losses = len(pnls) - wins
    gw = sum(x for x in pnls if x > 0)
    gl = -sum(x for x in pnls if x < 0)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    er = float(np.mean([t["r"] for t in trades]))
    run = peak = dd = 0.0
    for x in pnls:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    return {"n": len(trades), "w": wins, "l": losses, "pf": round(pf, 3),
            "pnl": round(sum(pnls), 2), "e_r": round(er, 4), "maxdd": round(dd, 2),
            "wr": round(100.0 * wins / len(trades), 1)}


def split_agg(trades, boundary_ts):
    """Aggregate trades by exit timestamp: IS (before boundary) / OOS."""
    out = {}
    for name, sel in (("is", lambda t: t["ts"] < boundary_ts),
                      ("oos", lambda t: t["ts"] >= boundary_ts),
                      ("all", lambda t: True)):
        out[name] = agg([t for t in trades if sel(t)])
    return out
