"""Parameter sweep driver for the custom backtest engine.

Uses the SAME sanctioned entry point the agent tool uses
(src.research.custom_strategy_runtime.run_custom_backtest), so every envelope
carries the v1_realistic_micros cost-model stamp, the dollar denominated P&L
and the fill_mode / promotion_grade stamps.

Usage:  .venv/bin/python research/cycle_20261030c/sweep.py <strategy.py> <tag>
"""
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath("."))

from src.research import custom_strategy_runtime as csr  # noqa: E402

STRAT = sys.argv[1] if len(sys.argv) > 1 else "research/cycle_20261030c/dol_draw_target_v1.py"
TAG = sys.argv[2] if len(sys.argv) > 2 else "base"
CODE = open(STRAT).read()

# grid: list of (label, params dict)
GRIDS = {
    "base": [
        ("base", {}),
    ],
}

RESULTS = []


def run(label, symbol, timeframe, params, days=90):
    t0 = time.time()
    try:
        m = csr.run_custom_backtest(
            code=CODE, symbol=symbol, days=days,
            timeframe=timeframe, params=params, qty=1,
            fill_mode="production", timeout_s=300,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  !! {label} {symbol} {timeframe} EXC {type(exc).__name__}: {exc}")
        return None
    dt = time.time() - t0
    if not isinstance(m, dict) or m.get("error"):
        print(f"  !! {label} {symbol} {timeframe} error={m.get('error') if isinstance(m, dict) else m}")
        return None
    row = {
        "label": label, "symbol": symbol, "tf": timeframe,
        "n": m.get("n_trades"), "pf": m.get("pf"), "pnl": m.get("total_pnl"),
        "wr": (100.0 * m.get("n_wins", 0) / m["n_trades"]) if m.get("n_trades") else 0.0,
        "er": m.get("expectancy_r"), "dd": m.get("max_drawdown"),
        "promotion_grade": m.get("promotion_grade"), "stale_pct": m.get("stale_fill_pct"),
        "slip": m.get("entry_slip_ticks_abs_mean"),
        "cost_model": m.get("cost_model"), "span_days": m.get("bars_span_days"),
        "n_bars": m.get("n_bars"), "max_gap_min": m.get("max_bar_gap_min"),
        "filled": m.get("n_entries_filled"), "unfilled": m.get("n_entries_unfilled"),
        "params": params, "secs": round(dt, 1),
    }
    RESULTS.append(row)
    print(f"  {label:34s} {symbol:4s} {timeframe:4s} n={row['n']:4} PF={row['pf']:6.3f} "
          f"PnL=${row['pnl']:10.2f} WR={row['wr']:5.1f}% E[R]={row['er']:+.3f} "
          f"pg={row['promotion_grade']} span={row['span_days']}d gap={row['max_gap_min']}m "
          f"slip={row['slip']} ({row['secs']}s)")
    return row


def main():
    grid = GRIDS.get(TAG)
    if grid is None:
        grid = json.loads(os.environ.get("SWEEP_GRID", "[]"))
        grid = [(g.get("label", "g"), g.get("params", {})) for g in grid]
    syms = os.environ.get("SYMS", "MGC,MNQ,MCL,ZF,6J").split(",")
    tfs = os.environ.get("TFS", "5m").split(",")
    for label, params in grid:
        for sym in syms:
            for tf in tfs:
                run(label, sym, tf, params)
    out = f"research/cycle_20261030c/sweep_{TAG}.json"
    with open(out, "w") as fh:
        json.dump(RESULTS, fh, indent=1, default=str)
    print("wrote", out)


if __name__ == "__main__":
    main()
