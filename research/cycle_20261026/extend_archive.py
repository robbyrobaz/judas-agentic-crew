"""EXTEND the deep bar archive BACKWARDS for a true out-of-sample segment.

WHY
---
The market-entry architecture (finding 9145) was tuned on the SAME 120-day
window the current archive holds (MGC 15m: 2026-06-11 -> 2026-10-23).  There is
therefore NO holdout behind the top cells.  A top-of-grid PF on the only data
we own is exactly the number the operator is right to distrust.

This script paginates IBKR history backwards on the SAME pinned contract the
live book trades (active_contracts.json -> 202612) and MERGES it into
`data/bars_archive/<SYM>_<tf>.parquet`.  `merge_into_archive` appends, so the
existing 120 days are preserved and the new segment sits strictly BEFORE it:
the older half becomes a genuine holdout for any parameter chosen on the
recent half.

Usage:
    .venv/bin/python research/cycle_20261026/extend_archive.py MGC 15m 340
    .venv/bin/python research/cycle_20261026/extend_archive.py MNQ 15m 340
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("src.bar_cache").setLevel(logging.WARNING)

from src import bar_archive  # noqa: E402

CONTRACT_MONTH = {
    "6J": "202612", "MGC": "202612", "MNQ": "202612",
    "MCL": "202612", "ZF": "202612",
}


async def main() -> int:
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    target_days = int(sys.argv[3]) if len(sys.argv) > 3 else 340
    summary = await bar_archive.backfill_symbol(
        symbol, tf,
        target_days=target_days,
        host="127.0.0.1", port=4002, client_id=4329,
        contract_month=CONTRACT_MONTH.get(symbol),
    )
    print(json.dumps(summary, indent=2, default=str))
    p = ROOT / "outputs" / f"archive_extend_{symbol}_{tf}.json"
    p.write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
