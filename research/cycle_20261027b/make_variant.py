"""Generate a parameter-baked VARIANT of the cycle-20261027b strategy template.

The house tool `run_custom_backtest(code=..., symbol=..., timeframe=...)` does
NOT accept a params dict -- it calls evaluate(bars, {}) -- so the parameters
of the cell we want to stamp have to be BAKED INTO THE CODE.  This script
writes that variant file (the template's DEFAULTS dict replaced by the cell).

Usage:
  .venv/bin/python research/cycle_20261027b/make_variant.py \
      research/cycle_20261027b/strategies/sweep_cont_market_entry_v2.py \
      '{"setup": "sweep_cont", "lookback": 32, ...}' \
      research/cycle_20261027b/strategies/mcl_sweep_cont_v1.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TPL = Path(sys.argv[1])
CELL = json.loads(sys.argv[2])
OUT = Path(sys.argv[3])

code = TPL.read_text()
new_defaults = "DEFAULTS = " + json.dumps(CELL, indent=4) + "\n"
code2, n = re.subn(r"DEFAULTS = \{.*?\n\}\n", new_defaults, code, count=1,
                   flags=re.S)
if n != 1:
    raise SystemExit(f"could not substitute DEFAULTS in {TPL} (n={n})")
OUT.write_text(code2)
print(f"wrote {OUT} with cell: {json.dumps(CELL, sort_keys=True)}")
