"""Fair-fill audit: re-measure library custom strategies under the
PRODUCTION entry-resolution model and compare against the legacy retro-fill
model.

Finding 9164: the legacy engine credited every entry at the price the
strategy derived, on a bar the strategy had already closed.  This script
runs the SAME code, on the SAME bars, through both fill models so the
collapse is measurable rather than asserted.

Usage:
    .venv/bin/python research/cycle_20261030a/fair_fill_audit.py --days 90
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.research import custom_strategy_runtime as csr  # noqa: E402

DB = "judas_crew.db"

# The library block from the 2026-10-30 briefing: best prior backtests per
# symbol, plus the retired-soft rows worth re-qualifying.
TARGETS = [
    # (symbol, timeframe, csid)
    ("ZF", "5m", 270), ("ZF", "5m", 403), ("ZF", "5m", 409), ("ZF", "15m", 237),
    ("6J", "15m", 203), ("6J", "15m", 333), ("6J", "5m", 276), ("6J", "5m", 262),
    ("MCL", "5m", 368), ("MCL", "5m", 194), ("MCL", "5m", 400), ("MCL", "15m", 354),
    ("MGC", "1h", 229), ("MGC", "5m", 348), ("MGC", "5m", 357), ("MGC", "5m", 257),
    ("MNQ", "5m", 295), ("MNQ", "5m", 401), ("MNQ", "15m", 332), ("MNQ", "5m", 253),
]


def load_code(csid: int):
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT code, name, symbol FROM custom_strategies WHERE id = ?", (csid,)
        ).fetchone()
    finally:
        conn.close()
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="outputs/fair_fill_audit_20261030a.json")
    args = ap.parse_args()

    # --- fetch bars once per (symbol, tf) --------------------------------
    bars_cache: dict[tuple[str, str], object] = {}
    for sym, tf, csid in TARGETS:
        key = (sym, tf)
        if key in bars_cache:
            continue
        t0 = time.time()
        df = csr._fetch_synthetic_or_live_bars(sym, args.days, tf)
        if df is None:
            print(f"!! no bars for {sym} {tf}", flush=True)
            bars_cache[key] = None
            continue
        bars_cache[key] = df
        span = ""
        try:
            span = f" span={(df['ts'].iloc[-1]-df['ts'].iloc[0]).days}d"
        except Exception:
            pass
        print(f"bars {sym} {tf}: n={len(df)}{span} ({time.time()-t0:.1f}s)", flush=True)

    results = []
    for sym, tf, csid in TARGETS:
        row = load_code(csid)
        if row is None:
            print(f"!! csid {csid} missing", flush=True)
            continue
        code, name, reg_symbol = row
        df = bars_cache.get((sym, tf))
        if df is None:
            continue
        entry = {"csid": csid, "name": name, "symbol": sym, "tf": tf,
                 "registered_symbol": reg_symbol}
        for mode in ("legacy", "production"):
            t0 = time.time()
            try:
                m = csr.run_custom_backtest_on_bars(
                    code=code, bars=df, params={}, symbol=sym, qty=1,
                    timeout_s=args.timeout, fill_mode=mode,
                )
            except Exception as exc:  # noqa: BLE001
                m = {"error": f"{type(exc).__name__}: {exc}"}
            entry[mode] = {k: m.get(k) for k in (
                "error", "n_signals", "n_trades", "n_wins", "n_losses",
                "total_pnl", "pf", "expectancy_r", "max_drawdown",
                "stale_fill_pct", "n_entries_filled", "n_entries_unfilled",
                "entry_slip_ticks_mean", "entry_slip_ticks_abs_mean",
                "cost_model", "n_bars", "bars_span_days", "max_bar_gap_min",
                "promotion_grade", "fill_model",
            )}
            entry[mode]["secs"] = round(time.time() - t0, 1)
        lg, pr = entry["legacy"], entry["production"]
        print(
            f"csid {csid:4d} {sym:3s} {tf:3s} {name[:38]:38s} | "
            f"legacy n={lg.get('n_trades')} pf={lg.get('pf')} ${lg.get('total_pnl')} | "
            f"PROD n={pr.get('n_trades')} pf={pr.get('pf')} ${pr.get('total_pnl')} "
            f"E[R]={pr.get('expectancy_r')} slip={pr.get('entry_slip_ticks_mean')} "
            f"stale%={pr.get('stale_fill_pct')}",
            flush=True,
        )
        results.append(entry)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
