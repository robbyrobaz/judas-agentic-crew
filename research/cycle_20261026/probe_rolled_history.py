"""PROBE: roll-aware, expired-contract intraday history from IBKR (paper).

WHY
---
The deep archive was backfilled PINNING the live contract (MGC 202612) with no
`includeExpired`, so every date before that contract had liquidity came back
as a continuous grid of STALE bars (OHLC all equal, volume 0).  That is the
evidence base every out-of-sample validation in this book has been leaning on.

This probe walks BACKWARDS in chunks for a GIVEN contract month and reports the
quality of what comes back:

    zero_range_pct : share of bars with high == low (a stale carry-forward bar)
    volume_gt0_pct : share of bars that actually traded

If a rolled (expired) contract returns real bars for the old window, then the
archive can be repaired and genuine holdouts become possible.

Usage:
  .venv/bin/python research/cycle_20261026/probe_rolled_history.py \
      MGC 15m 202512 60 20   # symbol tf contract_month lookback_days chunk_days
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SPECS = {
    "MGC": ("MGC", "COMEX"), "MNQ": ("MNQ", "CME"), "MCL": ("MCL", "NYMEX"),
    "ZF": ("ZF", "CBOT"), "6J": ("JPY", "CME"),
}
BAR_SIZE = {"5m": "5 mins", "15m": "15 mins", "1h": "1 hour"}


async def main():
    symbol = sys.argv[1].upper()
    tf = sys.argv[2]
    month = sys.argv[3] if len(sys.argv) > 3 else "202512"
    lookback = int(sys.argv[4]) if len(sys.argv) > 4 else 60
    chunk_days = int(sys.argv[5]) if len(sys.argv) > 5 else 20

    import ib_async
    ib = ib_async.IB()
    await ib.connectAsync("127.0.0.1", 4002, clientId=4399, timeout=25)
    try:
        sym, exch = SPECS[symbol]
        c = ib_async.Future(symbol=sym, exchange=exch,
                            lastTradeDateOrContractMonth=month,
                            includeExpired=True)
        q = await ib.qualifyContractsAsync(c)
        if not q or q[0] is None:
            print(json.dumps({"error": f"contract not found {symbol} {month}"}))
            return
        contract = q[0]
        print(f"contract {contract.localSymbol} conId={contract.conId}", flush=True)

        anchor = sys.argv[6] if len(sys.argv) > 6 else None
        cursor = (datetime.strptime(anchor, "%Y%m%d") if anchor
                  else datetime.now())
        rows = []
        all_bars = []
        for _ in range(max(1, lookback // chunk_days)):
            end_dt = cursor
            fmt = end_dt.strftime("%Y%m%d %H:%M:%S US/Eastern")
            try:
                bars = await asyncio.wait_for(
                    ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime=fmt,
                        durationStr=f"{chunk_days} D",
                        barSizeSetting=BAR_SIZE[tf],
                        whatToShow="TRADES",
                        useRTH=False,
                        formatDate=1,
                        keepUpToDate=False,
                    ), timeout=90)
            except Exception as exc:  # noqa: BLE001
                print(f"chunk end={fmt} ERROR {type(exc).__name__}: {exc}", flush=True)
                bars = []
            bars = bars or []
            if not bars:
                print(f"chunk end={fmt} -> 0 bars (end of history?)", flush=True)
                break
            n = len(bars)
            zero = sum(1 for b in bars if float(b.high) <= float(b.low))
            vol = sum(1 for b in bars if int(getattr(b, "volume", 0) or 0) > 0)
            rec = {"end": fmt, "n_bars": n, "first": str(bars[0].date),
                   "last": str(bars[-1].date),
                   "zero_range_pct": round(100.0 * zero / n, 1),
                   "volume_gt0_pct": round(100.0 * vol / n, 1),
                   "med_volume": sorted(int(getattr(b, "volume", 0) or 0) for b in bars)[n // 2]}
            rows.append(rec)
            print(json.dumps(rec), flush=True)
            for b in bars:
                all_bars.append({"ts": str(b.date), "o": float(b.open), "h": float(b.high),
                                 "l": float(b.low), "c": float(b.close),
                                 "v": int(getattr(b, "volume", 0) or 0)})
            cursor = cursor - timedelta(days=chunk_days)
            await asyncio.sleep(2.0)

        out = ROOT / "outputs" / f"probe_rolled_{symbol}_{tf}_{month}.json"
        out.write_text(json.dumps({"symbol": symbol, "tf": tf, "month": month,
                                   "chunks": rows, "n_bars": len(all_bars),
                                   "bars": all_bars[:50]}, indent=2, default=str))
        print(f"\nwrote {out} (bars kept={len(all_bars)})")
    finally:
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
