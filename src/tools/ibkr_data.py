"""IBKR data fetcher — CrewAI @tool wrapper.

Fetches 100 x 1H bars for a symbol from IBKR Gateway (port 4002).
Uses clientId=150 (data client, separate from execution clientId=151).

Returns JSON string with bars list, prior_high, prior_low.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from crewai.tools import tool

log = logging.getLogger(__name__)

_CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "MGC": {
        "exchange": "COMEX",
        "currency": "USD",
    },
    "MNQ": {
        "exchange": "CME",
        "currency": "USD",
    },
}


@tool("ibkr_data_tool")
def ibkr_data_tool(input_json: str) -> str:
    """Fetch 100 x 1H OHLCV bars for a symbol from IBKR paper account.

    Input JSON: {"symbol": "MGC"}

    Returns JSON string:
    {
      "bars": [{"ts": "2026-05-08T14:00:00", "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}, ...],
      "prior_high": float,
      "prior_low": float,
      "error": null | "error message"
    }

    prior_high and prior_low are the most recent completed trading day's high
    and low relative to the latest fetched bar date.
    """
    try:
        from ib_async import IB, ContFuture, util
    except ImportError:
        return json.dumps({"bars": [], "prior_high": 0.0, "prior_low": 0.0,
                           "error": "ib_async not installed"})

    try:
        data = json.loads(input_json)
        symbol = data.get("symbol", "MGC")
    except (json.JSONDecodeError, AttributeError):
        symbol = str(input_json).strip()

    spec = _CONTRACT_SPECS.get(symbol.upper(), _CONTRACT_SPECS["MGC"])

    try:
        import os
        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = int(os.environ.get("IBKR_PORT", "4002"))
        client_id = int(os.environ.get("IBKR_DATA_CLIENT_ID", "150"))

        ib = IB()
        util.startLoop()

        import asyncio

        async def _fetch() -> list[dict]:
            await ib.connectAsync(host, port, clientId=client_id, timeout=15)
            try:
                contract = ContFuture(
                    symbol=symbol.upper(),
                    exchange=spec["exchange"],
                )
                qualified = await ib.qualifyContractsAsync(contract)
                if not qualified:
                    raise ValueError(f"Could not qualify continuous contract for {symbol}")
                contract = qualified[0]

                bars = await ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="8 D",
                    barSizeSetting="1 hour",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=1,
                    keepUpToDate=False,
                )
                return bars
            finally:
                ib.disconnect()

        loop = asyncio.get_event_loop()
        raw_bars = loop.run_until_complete(_fetch())

        # Convert ib_async BarData objects to dicts
        bars_list = []
        for b in raw_bars:
            # b.date may be datetime or string
            if hasattr(b.date, "isoformat"):
                ts = b.date.isoformat()
                bar_date = b.date.date() if hasattr(b.date, "date") else None
            else:
                ts = str(b.date)
                try:
                    bar_date = datetime.strptime(ts[:10], "%Y-%m-%d").date()
                except ValueError:
                    bar_date = None

            bars_list.append({
                "ts": ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": int(b.volume) if b.volume is not None else 0,
                "_date": str(bar_date),
            })

        # Compute prior day high/low
        if bars_list:
            bars_list = bars_list[-100:]
            dated_bars = [b for b in bars_list if b["_date"] != "None"]
            if dated_bars:
                latest_bar_date = max(b["_date"] for b in dated_bars)
                prior_bars = [b for b in dated_bars if b["_date"] < latest_bar_date]
                if prior_bars:
                    max_prior_date = max(b["_date"] for b in prior_bars)
                    prior_day_bars = [b for b in prior_bars if b["_date"] == max_prior_date]
                    prior_high = max(b["high"] for b in prior_day_bars)
                    prior_low = min(b["low"] for b in prior_day_bars)
                else:
                    prior_high = max(b["high"] for b in bars_list[:-1]) if len(bars_list) > 1 else 0.0
                    prior_low = min(b["low"] for b in bars_list[:-1]) if len(bars_list) > 1 else 0.0
            else:
                prior_high = max(b["high"] for b in bars_list[:-1]) if len(bars_list) > 1 else 0.0
                prior_low = min(b["low"] for b in bars_list[:-1]) if len(bars_list) > 1 else 0.0
        else:
            prior_high = 0.0
            prior_low = 0.0

        # Strip internal _date field
        for b in bars_list:
            b.pop("_date", None)

        log.info("ibkr_data.fetched", extra={
            "symbol": symbol,
            "bar_count": len(bars_list),
            "prior_high": prior_high,
            "prior_low": prior_low,
        })

        return json.dumps({
            "bars": bars_list,
            "prior_high": prior_high,
            "prior_low": prior_low,
            "error": None,
        })

    except Exception as e:
        log.error("ibkr_data.error", extra={"symbol": symbol, "error": str(e)}, exc_info=True)
        return json.dumps({
            "bars": [],
            "prior_high": 0.0,
            "prior_low": 0.0,
            "error": str(e),
        })
