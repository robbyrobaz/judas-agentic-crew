"""Midnight-NY-open DAILY BIAS overlay harness.

Takes an EXISTING custom strategy (csid), replays it on real native bars with
the SAME fill semantics as src/research/custom_strategy_runtime.py, and then
measures what the "one line drawn at midnight" filter (YT X7kvlngAjY4) does to
its trade list:

    * price below the 00:00 New York opening price -> ONLY SELLS
    * price above the 00:00 New York opening price -> ONLY BUYS

The filter is applied AT SIGNAL TIME (not post-hoc) so the trade sequence is
exactly what a live strategy with the filter would produce.

Usage: .venv/bin/python research/tmp_bias_overlay.py <csid> <symbol> <tf> <days>
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from src.research import custom_strategy_runtime as csr  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import apply_costs_to_trade  # noqa: E402

DB = "judas_crew.db"


def load_code(csid):
    conn = sqlite3.connect(DB)
    row = conn.execute("SELECT code, name FROM custom_strategies WHERE id=?", (int(csid),)).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(f"csid {csid} not found")
    code = row[0]
    if isinstance(code, bytes):
        code = code.decode("utf-8", "ignore")
    return code, row[1]


def load_code_file(path):
    with open(path) as fh:
        return fh.read(), path


def ny_day_open_map(ts):
    s = pd.to_datetime(ts, utc=True)
    ny = s.dt.tz_convert("America/New_York")
    hours = ny.dt.hour.to_numpy()
    dates = ny.dt.date.to_numpy()
    opens = {}
    for i in range(len(ts)):
        d = dates[i]
        if d not in opens:
            opens[d] = (i, hours[i])
    return dates, hours, opens


def run(csid, symbol, timeframe, days, use_bias=True, target_r=None, code_file=None):
    if code_file:
        code, name = load_code_file(code_file)
    else:
        code, name = load_code(csid)
    bars = csr._fetch_synthetic_or_live_bars(symbol, days, timeframe)
    if bars is None or len(bars) < 50:
        print("NO BARS")
        return None
    fn, meta = csr._compile_and_load(code)
    if fn is None:
        print("LOAD FAIL", meta)
        return None

    spec = _CONTRACT_SPECS.get(symbol.upper())
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    ts = bars["ts"]
    o = bars["open"].values.astype(float)
    dates, hours, opens = ny_day_open_map(ts)

    trades = []
    open_trade = None
    n = len(bars)
    for i in range(2, n):
        if open_trade is not None:
            hi = float(bars["high"].iloc[i])
            lo = float(bars["low"].iloc[i])
            hit_stop = ((open_trade["direction"] == "long" and lo <= open_trade["stop"]) or
                        (open_trade["direction"] == "short" and hi >= open_trade["stop"]))
            hit_target = ((open_trade["direction"] == "long" and hi >= open_trade["target"]) or
                          (open_trade["direction"] == "short" and lo <= open_trade["target"]))
            if hit_stop or hit_target:
                entry = open_trade["entry"]
                risk = abs(entry - open_trade["stop"]) or 1.0
                won = hit_target and not hit_stop
                points = (abs(open_trade["target"] - entry) if won else -abs(entry - open_trade["stop"]))
                gross = points * dpp
                pnl = apply_costs_to_trade(symbol=symbol.upper(), gross_pnl=gross,
                                           exit_reason="target" if won else "stop", qty=1)
                trades.append({**open_trade, "pnl": float(pnl), "won": won,
                               "r": float(pnl / (risk * dpp))})
                open_trade = None
                continue
        sig = fn(bars.iloc[: i + 1], {})
        if not isinstance(sig, dict):
            continue
        direction = str(sig.get("direction", "")).lower()
        if direction not in ("long", "short"):
            continue
        try:
            entry = float(sig["entry"]); stop = float(sig["stop"]); target = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            continue
        if use_bias:
            d = dates[i]
            if d not in opens:
                continue
            idx0 = opens[d][0]
            day_open = float(o[idx0])
            close = float(bars["close"].iloc[i])
            if direction == "short" and not (close < day_open):
                continue
            if direction == "long" and not (close > day_open):
                continue
        open_trade = {"direction": direction, "entry": entry, "stop": stop,
                      "target": target, "ts": str(ts.iloc[i])}
    return {"name": name, "trades": trades, "bars": n,
            "span": (str(ts.iloc[0]), str(ts.iloc[-1]))}


def stats(trades):
    if not trades:
        return {"n": 0, "pf": 0.0, "wr": 0.0, "pnl": 0.0, "er": 0.0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    gw = sum(wins); gl = sum(losses)
    pf = (gw / gl) if gl > 0 else (999.0 if gw > 0 else 0.0)
    er = sum(t["r"] for t in trades) / len(trades)
    return {"n": len(trades), "pf": round(pf, 3),
            "wr": round(100.0 * len(wins) / len(pnls), 1),
            "pnl": round(sum(pnls), 2), "er": round(er, 4)}


if __name__ == "__main__":
    csid = sys.argv[1]
    symbol = sys.argv[2]
    tf = sys.argv[3] if len(sys.argv) > 3 else "5m"
    days = int(sys.argv[4]) if len(sys.argv) > 4 else 45
    for use_bias in (False, True):
        out = run(csid, symbol, tf, days, use_bias=use_bias)
        if out is None:
            continue
        tag = "BIAS " if use_bias else "BASE "
        print(f"{tag} {symbol} {tf} csid={csid} {out['name']} bars={out['bars']} "
              f"span={out['span'][0][:10]}..{out['span'][1][:10]} -> {stats(out['trades'])}")
