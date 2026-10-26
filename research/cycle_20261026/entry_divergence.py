"""ENTRY-DIVERGENCE MEASUREMENT for the market-entry architecture.

finding 47e2eeca sec.5.1 / the registrar's gate-4 screen charge the OBSERVED
adverse entry divergence (p50 / p90 ticks) on top of a backtest envelope.
For the legacy families that divergence is huge (MGC p50 = 21.5 ticks) because
the named entry is a STRUCTURAL level far from the decision bar.

For the market-entry architecture the named entry IS the decision bar's close,
so the only divergence left is the gap to the next bar's open -- the price the
broker would actually fill at.  This script measures it per symbol.

Usage: .venv/bin/python research/cycle_20261026/entry_divergence.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402

SYMS = ("MGC", "MNQ", "MCL", "ZF", "6J")
TFS = ("15m", "5m")


def main() -> int:
    out = {}
    for sym in SYMS:
        spec = _CONTRACT_SPECS.get(sym) or {}
        tick = float(spec.get("tick", 0.1) or 0.1)
        out[sym] = {"tick": tick, "tick_value": float(spec.get("tick_value", 1.0) or 1.0)}
        for tf in TFS:
            p = ROOT / "data" / "bars_archive" / f"{sym}_{tf}.parquet"
            if not p.exists():
                continue
            df = pd.read_parquet(p).sort_values("ts").reset_index(drop=True)
            c = df["close"].to_numpy(float)
            o = df["open"].to_numpy(float)
            d = np.abs(o[1:] - c[:-1]) / tick      # adverse ticks, next open vs prior close
            d = d[np.isfinite(d)]
            out[sym][tf] = {
                "n": int(len(d)),
                "p50_ticks": round(float(np.percentile(d, 50)), 3),
                "p90_ticks": round(float(np.percentile(d, 90)), 3),
                "p99_ticks": round(float(np.percentile(d, 99)), 3),
                "mean_ticks": round(float(d.mean()), 3),
            }
    print(json.dumps(out, indent=2))
    (ROOT / "outputs" / "entry_divergence_20261026.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
