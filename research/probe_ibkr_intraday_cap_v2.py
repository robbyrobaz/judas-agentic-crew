"""Probe IBKR for the ACTUAL per-request intraday duration cap (correct contract).

v2 of the probe. Two fixes over v1:
  1. Uses bar_cache._pick_contract (HIGHEST-VOLUME contract) instead of the
     nearest expiry. The nearest-expiry contract (e.g. MGCV6) has sparse
     history and under-reports what IBKR will actually serve.
  2. Long per-request timeout (180s). A TIMEOUT on a big request means
     "still working", NOT "rejected" — IBKR signals a genuine cap error
     (162 "cannot exceed X days") FAST. So we must not conflate slow with
     rejected, which is what the v1 45s timeout did.

Run:  .venv/bin/python research/probe_ibkr_intraday_cap_v2.py
"""
from __future__ import annotations

import asyncio
import json
import time

from ib_async import IB, Future

HOST, PORT, CID = "127.0.0.1", 4002, 4328
BAR_SIZE = {"5m": "5 mins", "15m": "15 mins", "1h": "1 hour"}
PROBES = [
    ("5m", "10 D"),   # current setting — baseline
    ("5m", "20 D"),
    ("5m", "30 D"),
    ("5m", "60 D"),
    ("15m", "20 D"),  # current setting — baseline
    ("15m", "30 D"),
    ("15m", "60 D"),
    ("1h", "180 D"),
]


async def _probe(ib, contract, tf: str, dur: str, per_req_timeout: float = 180.0) -> dict:
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
        span = f"{bars[0].date} -> {bars[-1].date}" if n else ""
        return {"tf": tf, "duration": dur, "status": "OK", "bars": n,
                "span": span, "elapsed_s": round(time.time() - t0, 2)}
    except asyncio.TimeoutError:
        return {"tf": tf, "duration": dur, "status": "TIMEOUT", "bars": 0,
                "span": "", "elapsed_s": round(time.time() - t0, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"tf": tf, "duration": dur, "status": "FAIL", "bars": 0,
                "span": f"{type(exc).__name__}: {str(exc)[:250]}",
                "elapsed_s": round(time.time() - t0, 2)}


async def main() -> None:
    ib = IB()
    try:
        await ib.connectAsync(HOST, PORT, clientId=CID, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print("CONNECT_FAIL", exc)
        return
    try:
        # Same contract the LIVE cache uses: highest-volume, not nearest expiry.
        contract, month, err = await _pick_contract(ib)
        if err:
            print("PICK_FAIL", err)
            return
        print(f"USING contract={contract.localSymbol} month={month} "
              f"ltd={contract.lastTradeDateOrContractMonth} conId={contract.conId}", flush=True)

        out = []
        for tf, dur in PROBES:
            rec = await _probe(ib, contract, tf, dur)
            rec["contract"] = contract.localSymbol
            out.append(rec)
            print(json.dumps(rec), flush=True)
            await asyncio.sleep(2.0)

        with open("outputs/ibkr_intraday_cap_probe_v2.json", "w") as fh:
            json.dump(out, fh, indent=2)
        print("WROTE outputs/ibkr_intraday_cap_probe_v2.json", flush=True)
    finally:
        ib.disconnect()


async def _pick_contract(ib):
    """Mirror bar_cache's highest-volume roll (minus the memo)."""
    import sys
    sys.path.insert(0, ".")
    from src import bar_cache
    spec = bar_cache._CONTRACT_SPECS["MGC"]
    return await bar_cache._pick_contract(ib, spec, "MGC")


if __name__ == "__main__":
    asyncio.run(main())