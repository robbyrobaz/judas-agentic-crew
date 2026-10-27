"""Timing calibration for the multi-symbol market-entry sweep (2026-10-26)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261025"))

from honest_screen import load_bars, metrics, run_books  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261025" / "strategies" /
        "market_entry_ict_v1.py").read_text()

CFG = {"setup": "sweep_cont", "lookback": 12, "min_sweep_atr": 0.30,
       "disp_k": 1.0, "rr": 1.5, "confirm_bars": 6, "body_ratio_min": 0.55,
       "stop_buf_atr": 0.15, "atr_period": 14}

for sym, tf, days in (("MNQ", "15m", 0), ("MNQ", "15m", 120),
                      ("MGC", "5m", 45)):
    bars = load_bars(sym, tf, days=days)
    t0 = time.time()
    b, lv, fa, meta = run_books(CODE, bars, CFG, sym)
    dt = time.time() - t0
    print(f"{sym} {tf} days={days} bars={len(bars)} "
          f"loop={meta.get('loop_s')}s wall={dt:.1f}s "
          f"live={metrics(lv)} base={metrics(b)}", flush=True)
