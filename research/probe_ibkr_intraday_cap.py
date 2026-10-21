"""Probe IBKR for the ACTUAL per-request intraday duration cap.

Finding 9105 claims the 5m/15m evidence base is limited to 13-27 days because
src/bar_cache.py only asks for "10 D" / "20 D". This script checks whether that
is an IBKR hard cap or a self-imposed one, by asking for progressively longer
durations and recording what comes back (IBKR raises error 162 when a request
exceeds its cap).

Run:  .venv/bin/python research/probe_ibkr_intraday_cap.py
"""
from __future__ import annotations

import asyncio
import json
import time

from ib_async import IB, Future

# Client IDs 150/151 are live (scanner/exec) and 159 is the research backtest
# path; use a high, unlikely-to-collide id for probing.
HOST, PORT, CID = "127.0.0.1", 4002, 4327
BAR_SIZE = {"5m": "5 mins", "15m": "15 mins", "1h": "1 hour"}
# (tf, durationStr) pairs to probe, cheapest first.
PROBES = [
    ("5m", "5 D"),
    ("5m", "10 D"),
    ("5m", "30 D"),
    ("5m", "60 D"),
    ("15m", "20 D"),
    ("15m", "60 D"),
    ("1h", "60 D"),
    ("1h", "180 D"),
]


async def _probe(ib, contract, tf: str, dur: str, per_req_timeout: float = 45.0) -> dict:
    t0 = time.time()
    try:
        bars = await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=dur,
                barSizeSetting=BAR_SIZE[tf],
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            ),
            timeout=per_req_timeout,
        )
        n = len(bars) if bars else 0
        span = ""
        if n:
            first = bars[0].date
            last = bars[-1].date
            span = f"{first} -> {last}"
        return {
            "tf": tf, "duration": dur, "status": "OK", "bars": n,
            "span": span, "elapsed_s": round(time.time() - t0, 2),
        }
    except asyncio.TimeoutError:
        return {
            "tf": tf, "duration": dur, "status": "TIMEOUT", "bars": 0,
            "span": "", "elapsed_s": round(time.time() - t0, 2),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "tf": tf, "duration": dur, "status": "FAIL", "bars": 0,
            "span": f"{type(exc).__name__}: {str(exc)[:200]}",
            "elapsed_s": round(time.time() - t0, 2),
        }


async def main() -> None:
    ib = IB()
    try:
        await ib.connectAsync(HOST, PORT, clientId=CID, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print("CONNECT_FAIL", exc)
        return
    try:
        cds = await ib.reqContractDetailsAsync(
            Future(symbol="MGC", exchange="COMEX", includeExpired=False))
        if not cds:
            print("NO_CONTRACT")
            return
        cds_sorted = sorted(cds, key=lambda cd: cd.contract.lastTradeDateOrContractMonth or "")
        contract = cds_sorted[0].contract
        print(f"USING contract={contract.localSymbol} "
              f"ltd={contract.lastTradeDateOrContractMonth} conId={contract.conId}")

        out = []
        for tf, dur in PROBES:
            rec = await _probe(ib, contract, tf, dur)
            out.append(rec)
            print(json.dumps(rec), flush=True)
            await asyncio.sleep(1.0)

        with open("outputs/ibkr_intraday_cap_probe.json", "w") as fh:
            json.dump(out, fh, indent=2)
        print("WROTE outputs/ibkr_intraday_cap_probe.json")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())