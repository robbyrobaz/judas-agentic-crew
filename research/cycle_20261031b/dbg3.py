"""Find the exception the sandbox swallows."""
from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

logging.basicConfig(level=logging.DEBUG)

from explore_prod import load_bars  # noqa: E402
from src.research import custom_strategy_runtime as csr  # noqa: E402

CODE = (ROOT / "research" / "cycle_20261031a" /
        "market_entry_sweep_cont_london_15m_v1.py").read_text()
bars = load_bars("MGC", "15m", 20)
fn, meta = csr._compile_and_load(CODE)
print("loaded:", fn is not None, meta)
sl = bars.iloc[:2000]
try:
    sig = fn(sl, {})
    print("sig:", sig)
except Exception:
    traceback.print_exc()
