"""Smoke test for the Zen sweep->FVG->IFVG architecture (2026-10-28 cycle).

Runs the strategy over one clean segment of each symbol/timeframe and reports
signal counts + wall-clock per bar, so the screen budget can be sized.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261027"))

import numpy as np  # noqa: E402

from seg_sweep import clean_segments  # noqa: E402
from src.research.custom_strategy_runtime import _compile_and_load  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261028" / "strategies" /
        "market_entry_sweep_ifvg_v1.py").read_text()

CELLS = [
    {"lookback": 12, "min_sweep_atr": 0.30, "fvg_atr_min": 0.15,
     "fvg_max_age": 12, "ifvg_edge": "near", "struct_min_pools": 0,
     "rr": 1.5, "confirm_bars": 6, "disp_k": 0.8, "body_ratio_min": 0.55,
     "stop_buf_atr": 0.15, "atr_period": 14},
    {"lookback": 12, "min_sweep_atr": 0.30, "fvg_atr_min": 0.15,
     "fvg_max_age": 12, "ifvg_edge": "far", "struct_min_pools": 0,
     "rr": 1.5, "confirm_bars": 6, "disp_k": 0.8, "body_ratio_min": 0.55,
     "stop_buf_atr": 0.15, "atr_period": 14},
    {"lookback": 20, "min_sweep_atr": 0.15, "fvg_atr_min": 0.10,
     "fvg_max_age": 16, "ifvg_edge": "near", "struct_min_pools": 2,
     "rr": 1.5, "confirm_bars": 6, "disp_k": 0.8, "body_ratio_min": 0.55,
     "stop_buf_atr": 0.15, "atr_period": 14},
]


def main() -> int:
    fn, meta = _compile_and_load(CODE)
    if fn is None:
        print("LOAD ERROR", meta)
        return 1
    for symbol, tf in (("MGC", "15m"), ("MGC", "5m"), ("MNQ", "15m"),
                       ("ZF", "15m"), ("MCL", "15m"), ("6J", "15m")):
        segs = clean_segments(symbol, tf)
        if not segs:
            print(f"{symbol} {tf}: no clean segment")
            continue
        seg = max(segs, key=len)
        for cell in CELLS:
            t0 = time.time()
            n_sig = 0
            err = 0
            n = len(seg)
            step = max(1, n // 4000)
            idxs = list(range(60, n, step))
            for i in idxs:
                try:
                    sig = fn(seg.iloc[max(0, i - 400):i + 1], dict(cell))
                except Exception as exc:  # noqa: BLE001
                    err += 1
                    if err < 3:
                        print("   ERR", type(exc).__name__, exc)
                    continue
                if isinstance(sig, dict):
                    n_sig += 1
            dt = time.time() - t0
            per_bar = dt / max(1, len(idxs))
            print(f"{symbol} {tf} seg={len(seg)} edge={cell['ifvg_edge']} "
                  f"struct={cell['struct_min_pools']} lb={cell['lookback']}: "
                  f"sigs={n_sig}/{len(idxs)} ({100.0*n_sig/max(1,len(idxs)):.2f}%) "
                  f"err={err} {per_bar*1e6:.0f}us/bar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
