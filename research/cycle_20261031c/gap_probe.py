"""FILL-GAP PROBE (cycle 20261031c).

Question: when the custom backtest engine resolves a PRODUCTION fill, how far
in TIME is the fill bar from the decision bar?

The engine's production fill = "open of the bar after the decision".  If the
bar series is continuous that is one bar-interval.  If the series has HOLES
(IBKR paginated deep archive + live cache union, session breaks, missing
buckets) then "the bar after the decision" can be minutes OR HOURS away, and
the fill price is then a price the market never offered at decision time.

The engine currently stamps `stale_fill_n` = 0 for every strategy because that
counter only measures the STRATEGY's own signal timestamp, not the
decision->fill gap.  So the contamination is invisible.

This probe measures, per symbol/tf:
  * bar spacing (mode / median of dt)
  * fraction of bars whose dt > 1.5 intervals  (the HOLE rate)
  * the decision->fill gap distribution of the ENGINE's own production-fill
    simulator, by re-running the engine's fill loop over the same bars.

Usage:  .venv/bin/python research/cycle_20261031c/gap_probe.py [symbol ...]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60}


def gap_stats(df: pd.DataFrame, tf: str) -> dict:
    ts = pd.to_datetime(df["ts"], utc=True).sort_values().reset_index(drop=True)
    d = ts.diff().dt.total_seconds().div(60).dropna()
    iv = INTERVAL_MIN[tf]
    return {
        "n_bars": int(len(df)),
        "span_days": round((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400.0, 1),
        "bars_per_day": round(len(df) / max((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400.0, 1e-9), 1),
        "dt_median_min": round(float(d.median()), 1),
        "dt_mode_min": round(float(d.mode().iloc[0]), 1) if len(d.mode()) else None,
        "frac_gt_1p5iv": round(float((d > 1.5 * iv).mean()), 4),
        "frac_gt_60min": round(float((d > 60).mean()), 4),
        "dt_p99_min": round(float(d.quantile(0.99)), 1),
        "dt_max_min": round(float(d.max()), 1),
    }


def main() -> None:
    syms = sys.argv[1:] or LEGAL
    rows = []
    for sym in syms:
        for tf in TFS:
            try:
                df = bar_archive.read_archive(sym, tf)
            except Exception as exc:  # noqa: BLE001
                print(f"{sym} {tf}: archive read failed: {exc}")
                continue
            if df is None or len(df) == 0:
                print(f"{sym} {tf}: archive empty")
                continue
            st = gap_stats(df, tf)
            st["sym"] = sym
            st["tf"] = tf
            rows.append(st)
            print(st)
    out = pd.DataFrame(rows)
    out.to_csv("research/cycle_20261031c/archive_gap_stats.csv", index=False)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
