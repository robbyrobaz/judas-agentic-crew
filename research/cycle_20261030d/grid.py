"""Grid driver with a TRAIN/TEST split, using the sanctioned backtest engine.

Fetches each (symbol, timeframe) bar series ONCE, then replays every
parameter variant on the TRAIN slice (first `train_frac` of the bars) and the
TEST slice (the rest) with fill_mode='production' (market at the open of the
bar after the decision), cost_model=v1_realistic_micros, qty=1.

Usage: .venv/bin/python research/cycle_20261030d/grid.py grid.json
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath("."))
from src.research import custom_strategy_runtime as csr  # noqa: E402

TICK = {"MGC": 0.10, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.0078125, "6J": 0.0000005}
_BAR_CACHE = {}


def get_bars(symbol, days, tf):
    key = (symbol, days, tf)
    if key not in _BAR_CACHE:
        _BAR_CACHE[key] = csr._fetch_synthetic_or_live_bars(symbol, days, tf)
    return _BAR_CACHE[key]


def main():
    spec = json.load(open(sys.argv[1]))
    train_frac = float(spec.get("train_frac", 0.55))
    code_cache = {}
    rows = []
    for cell in spec["cells"]:
        f = cell["file"]
        if f not in code_cache:
            code_cache[f] = open(f).read()
        code = code_cache[f]
        sym = cell["symbol"]
        tf = cell.get("timeframe", "15m")
        days = int(cell.get("days", 120))
        bars = get_bars(sym, days, tf)
        if bars is None or len(bars) < 200:
            print("no bars", sym, tf); continue
        cut = int(len(bars) * train_frac)
        splits = {"train": bars.iloc[:cut].reset_index(drop=True),
                  "test": bars.iloc[cut:].reset_index(drop=True)}
        for label, params in cell["variants"]:
            p = dict(params)
            p.setdefault("tick", TICK[sym])
            for sname, sub in splits.items():
                t0 = time.time()
                m = csr.run_custom_backtest_on_bars(
                    code=code, bars=sub, symbol=sym, params=p, qty=1,
                    fill_mode="production", timeout_s=600)
                rec = {
                    "file": os.path.basename(f), "label": label, "sym": sym, "tf": tf,
                    "split": sname, "n": m.get("n_trades"),
                    "pf": round(float(m.get("pf", 0)), 3),
                    "pnl": round(float(m.get("total_pnl", 0)), 2),
                    "er": round(float(m.get("expectancy_r", 0)), 3),
                    "wr": round(100.0 * m.get("n_wins", 0) / max(1, m.get("n_trades", 0)), 1),
                    "dd": round(float(m.get("max_drawdown", 0)), 1),
                    "grade": m.get("promotion_grade"), "stale": m.get("stale_fill_n"),
                    "slip": m.get("entry_slip_ticks_abs_mean"),
                    "span": m.get("bars_span_days"), "s": round(time.time() - t0, 1),
                    "params": params,
                }
                rows.append(rec)
                print(json.dumps(rec), flush=True)
    out = sys.argv[1].replace(".json", "_out.json")
    with open(out, "w") as fh:
        json.dump(rows, fh, indent=1)
    print("wrote", out)


if __name__ == "__main__":
    main()
