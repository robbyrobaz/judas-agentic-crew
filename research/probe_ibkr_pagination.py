"""Can we paginate BACKWARDS past IBKR's single-request intraday cap?

Motivation
----------
Finding 9105 concluded the 5m/15m evidence base is permanently 13-27 days wide
because src/bar_cache.py asks for "10 D" (5m) / "20 D" (15m). My probe confirms a
single request for 20 D / 30 D of 5m returns 0 bars -- IBKR's internal ~60s
request timeout kills it ("query cancelled", error 162). But that is a limit on
the SIZE OF ONE REQUEST, not on how much history exists. IBKR accepts an
explicit ``endDateTime``, so we can walk backwards in small chunks and stitch
them into a deep archive.

This script tests exactly that: N successive 9-day chunks anchored at an
explicit endDateTime, walking back ~90 days, on the live contract.

If it succeeds, the fix is a paginated deep-backfill that populates a durable
bar archive, and every 5m/15m backtest in the book can finally be measured over
a real window instead of 13 days.

Run:  .venv/bin/python research/probe_ibkr_pagination.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta

from ib_async import IB

HOST, PORT, CID = "127.0.0.1", 4002, 4329
CHUNK_DAYS = 9
TARGET_DAYS = 90
SYMBOL = "MGC"
BAR_SIZE = {"5m": "5 mins", "15m": "15 mins"}


async def _fetch_chunk(ib, contract, tf: str, chunk_days: int, end_dt: datetime,
                       per_req_timeout: float = 75.0) -> list:
    """One historical request anchored at an explicit endDateTime."""
    fmt = end_dt.strftime("%Y%m%d %H:%M:%S")
    try:
        return await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                contract,
                endDateTime=fmt,
                durationStr=f"{chunk_days} D",
                barSizeSetting=BAR_SIZE[tf],
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            ),
            timeout=per_req_timeout,
        ) or []
    except Exception as exc:  # noqa: BLE001
        print(f"  chunk FAIL end={fmt} err={type(exc).__name__}: {str(exc)[:120]}", flush=True)
        return []


async def main() -> None:
    tf = sys.argv[1] if len(sys.argv) > 1 else "5m"
    chunk_days = int(sys.argv[2]) if len(sys.argv) > 2 else CHUNK_DAYS

    ib = IB()
    try:
        await ib.connectAsync(HOST, PORT, clientId=CID, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print("CONNECT_FAIL", exc)
        return
    try:
        sys.path.insert(0, ".")
        from src import bar_cache
        contract, month, err = await bar_cache._pick_contract(ib, bar_cache._CONTRACT_SPECS[SYMBOL], SYMBOL)
        if err:
            print("PICK_FAIL", err)
            return
        print(f"SYMBOL={SYMBOL} tf={tf} contract={contract.localSymbol} month={month} "
              f"chunk={chunk_days}D target={TARGET_DAYS}D", flush=True)

        seen: dict[str, dict] = {}
        chunks = []
        cursor = datetime.now()

        n_chunks = TARGET_DAYS // chunk_days
        for i in range(n_chunks):
            t0 = time.time()
            bars = await _fetch_chunk(ib, contract, tf, chunk_days, cursor)
            added = 0
            for b in bars:
                key = b.date.isoformat()
                if key not in seen:
                    seen[key] = {
                        "ts": key, "open": float(b.open), "high": float(b.high),
                        "low": float(b.low), "close": float(b.close),
                        "volume": int(b.volume or 0),
                    }
                    added += 1
            rec = {"chunk": i, "end_dt": cursor.isoformat(), "bars_returned": len(bars),
                   "bars_added": added, "cumulative": len(seen), "elapsed_s": round(time.time() - t0, 1)}
            chunks.append(rec)
            print(json.dumps(rec), flush=True)
            if not bars:
                print("  EMPTY CHUNK - stopping", flush=True)
                break
            # walk back past this chunk's start
            cursor = cursor - timedelta(days=chunk_days)
            await asyncio.sleep(2.0)

        rows = sorted(seen.values(), key=lambda r: r["ts"])
        span_days = 0
        if rows:
            try:
                a = datetime.fromisoformat(rows[0]["ts"])
                b = datetime.fromisoformat(rows[-1]["ts"])
                span_days = (b - a).days
            except Exception:  # noqa: BLE001
                pass
        summary = {
            "symbol": SYMBOL, "timeframe": tf, "contract": contract.localSymbol,
            "chunk_days": chunk_days, "n_chunks_run": len(chunks),
            "total_bars": len(rows), "span_days": span_days,
            "first": rows[0]["ts"] if rows else None,
            "last": rows[-1]["ts"] if rows else None,
            "chunks": chunks,
        }
        print("\n=== SUMMARY ===")
        print(json.dumps({k: v for k, v in summary.items() if k != "chunks"}, indent=2), flush=True)

        tag = f"{SYMBOL}_{tf}_chunk{chunk_days}"
        with open(f"outputs/ibkr_pagination_{tag}.json", "w") as fh:
            json.dump({"summary": summary, "bars": rows}, fh, indent=2)
        print(f"WROTE outputs/ibkr_pagination_{tag}.json", flush=True)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())