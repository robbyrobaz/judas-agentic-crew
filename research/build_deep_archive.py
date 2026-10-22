"""Build the deep bar archive for every legal symbol at 5m and 15m.

Usage:  .venv/bin/python research/build_deep_archive.py [SYMBOLS...] [--tf 5m,15m]
        .venv/bin/python research/build_deep_archive.py MNQ MCL --tf 5m

Symbols are POSITIONAL and repeatable; the timeframe is only ever read from an
explicit --tf flag. (The old positional form treated the 2nd arg as a timeframe,
so "MNQ MCL" silently backfilled MNQ at 1h.)

Runs sequentially (IBKR paces historical requests) and writes a summary to
outputs/deep_archive_summary.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Connecting to the IBKR gateway subscribes us to the account feed; ib_async
# logs every fill/commission at INFO, which drowns the backfill progress.
# Only real problems (error 162 / 10314) matter here.
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("src.bar_cache").setLevel(logging.WARNING)

# Allow `python research/build_deep_archive.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bar_archive  # noqa: E402

LEGAL_SYMBOLS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
# 1h is in scope: `bar_archive.BAR_SIZE`/`DEFAULT_CHUNK_DAYS` already carry it,
# but this script rejected it, so MNQ_1h.parquet only existed by hand and ZF/6J
# had no 1h archive at all. Any timeframe `bar_archive` understands is legal here.
TIMEFRAMES = ["5m", "15m", "1h"]
TARGET_DAYS = 120


def parse_args(argv):
    """-> (symbols, timeframes). Unknown symbol/timeframe is a hard error.

    Previously this was positional (`script MGC 5m`), so any list of symbols
    silently collapsed to one symbol + one timeframe, and the extra symbols were
    normalized to 1h -- a backfill of the WRONG timeframe that still looked
    like it worked.
    """
    symbols: list[str] = []
    tfs: list[str] = list(TIMEFRAMES)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tf":
            i += 1
            if i >= len(argv):
                raise SystemExit("--tf needs a value, e.g. --tf 5m or --tf 5m,15m")
            tfs = [x.strip() for x in argv[i].split(",") if x.strip()]
        elif a.startswith("--tf="):
            tfs = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
        elif not a.startswith("-"):
            symbols.append(a.upper())
        i += 1

    if not symbols:
        symbols = list(LEGAL_SYMBOLS)

    bad_t = [t for t in tfs if t not in TIMEFRAMES]
    if bad_t:
        raise SystemExit(f"unsupported timeframe(s) {bad_t}; expected any of {TIMEFRAMES}")
    bad_s = [s for s in symbols if s not in LEGAL_SYMBOLS]
    if bad_s:
        raise SystemExit(f"unsupported symbol(s) {bad_s}; expected any of {LEGAL_SYMBOLS}")
    return symbols, tfs


async def main() -> None:
    symbols, tfs = parse_args(sys.argv[1:])
    print(f"Building deep archive for {symbols} at {tfs}", flush=True)

    out = []
    for i, sym in enumerate(symbols):
        for j, tf in enumerate(tfs):
            # Distinct client id per call so a stale socket never poisons the next.
            cid = 4400 + i * 10 + j
            print(f"\n=== BACKFILL {sym} {tf} (client {cid}) ===", flush=True)
            t0 = time.time()
            try:
                res = await bar_archive.backfill_symbol(
                    sym, tf, target_days=TARGET_DAYS, client_id=cid)
            except Exception as exc:  # noqa: BLE001
                res = {"error": f"{type(exc).__name__}: {exc}"}
            res["wall_s"] = round(time.time() - t0, 1)
            out.append(res)
            print(json.dumps(res, indent=2), flush=True)
            with open("outputs/deep_archive_summary.json", "w") as fh:
                json.dump(out, fh, indent=2)

    print("\n=== DONE ===")
    for r in out:
        print(f"{r.get('symbol','?')} {r.get('timeframe','?')}: "
              f"{r.get('total_bars','?')} bars / {r.get('span_days','?')}d "
              f"{r.get('error','')}")


if __name__ == "__main__":
    asyncio.run(main())