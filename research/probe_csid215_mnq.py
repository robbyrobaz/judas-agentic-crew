"""Probe: why does active row #4843 (MNQ, csid 215) produce ZERO signals?

Replays the csid-215 code over the archived MNQ 5m bars using the SAME
convention as the live scanner (evaluate() sees the full bar history, the
decision is the LAST bar) and reports how often the code returns a signal.
"""
from __future__ import annotations

import sqlite3
import sys

import pandas as pd

sys.path.insert(0, ".")

from src.research.custom_strategy_runtime import evaluate_custom_strategy  # noqa: E402

DB = "judas_crew.db"
conn = sqlite3.connect(DB)
row = conn.execute(
    "SELECT code, symbol, backtest_metrics_json FROM custom_strategies WHERE id=215"
).fetchone()
code = row[0]
params = {
    "timeframe": "5m",
    "sweep_lookback": 40,
    "fvg_lookback": 28,
    "min_gap_factor": 0.1,
    "rr": 2.0,
    "zone_buffer": 0.2,
    "symbol": "MNQ",
    "tick": 0.25,
    "dollars_per_point": 2,
    "__csid__": 215,
}

bars = pd.read_parquet("data/bars_archive/MNQ_5m.parquet")
print("bars:", len(bars), "cols:", list(bars.columns))
if "ts" in bars.columns:
    print("span:", bars["ts"].iloc[0], "->", bars["ts"].iloc[-1])

n_fires = 0
first_fires = []
# LIVE convention: evaluate() gets the whole history, decision = last bar.
for i in range(60, len(bars)):
    window = bars.iloc[: i + 1]
    sig = evaluate_custom_strategy(code=code, bars=window, params=params)
    if sig:
        n_fires += 1
        if len(first_fires) < 12:
            first_fires.append((str(window["ts"].iloc[-1]), sig.get("direction"),
                                sig.get("entry"), sig.get("stop"), sig.get("target")))
print(f"LIVE-convention fires over archive: {n_fires}")
for f in first_fires:
    print("   ", f)

# Where does the gate stop it?  Instrument the session gate + sweep gate.
ts = bars["ts"]
if getattr(ts.dt, "tz", None) is None:
    ts_utc = ts.dt.tz_localize("UTC")
else:
    ts_utc = ts.dt.tz_convert("UTC")
hours = ts_utc.dt.hour
in_session = ((hours >= 7) & (hours < 10)) | ((hours >= 13) & (hours < 16))
print("bars in session window:", int(in_session.sum()), "of", len(bars))
