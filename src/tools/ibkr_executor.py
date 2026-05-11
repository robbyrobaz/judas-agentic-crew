"""IBKR order executor — CrewAI @tool wrapper.

Places orders via IBKR Gateway (port 4002 — paper-account-only Gateway).
Uses clientId=151 (execution client, separate from data clientId=150).

The IBKR account itself is paper (DUH...). The mode field on
config.yaml is informational and no longer gates execution.
"""
from __future__ import annotations

import json
import logging
import os

from crewai.tools import tool

log = logging.getLogger(__name__)

# Contract specs (same as ibkr_data.py — no shared imports)
_CONTRACT_SPECS: dict[str, dict] = {
    "MGC": {
        "exchange": "COMEX",
        "currency": "USD",
    },
    "MNQ": {
        "exchange": "CME",
        "currency": "USD",
    },
}


def _assert_paper_mode() -> None:
    """No-op kept for back-compat. The IBKR account is paper; no need
    to assert it at every order call."""
    return


@tool("ibkr_executor_tool")
def ibkr_executor_tool(input_json: str) -> str:
    """Place a paper market order on IBKR for a futures contract.

    PAPER MODE ONLY — will raise if config mode is not 'paper'.

    Input JSON:
    {
      "symbol": "MGC",
      "direction": "long" | "short",
      "qty": 1,
      "entry": 3220.50,
      "stop": 3225.80,
      "target": 3210.00
    }

    Returns JSON:
    {
      "ibkr_order_id": int,
      "status": "placed" | "error",
      "message": str
    }

    Note: Places a market order for entry. Stop and target are recorded
    to the database by TradeExecutor for manual bracket management in Phase 1.
    """
    _assert_paper_mode()

    try:
        from ib_async import IB, ContFuture, Future, MarketOrder, util
    except ImportError:
        return json.dumps({
            "ibkr_order_id": -1,
            "status": "error",
            "message": "ib_async not installed",
        })

    try:
        data = json.loads(input_json)
        symbol = data["symbol"].upper()
        direction = data["direction"].lower()
        qty = int(data.get("qty", 1))
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return json.dumps({
            "ibkr_order_id": -1,
            "status": "error",
            "message": f"Invalid input: {e}",
        })

    if direction not in ("long", "short"):
        return json.dumps({
            "ibkr_order_id": -1,
            "status": "error",
            "message": f"direction must be 'long' or 'short', got '{direction}'",
        })

    if qty != 1:
        return json.dumps({
            "ibkr_order_id": -1,
            "status": "error",
            "message": f"qty={qty} out of valid range [1, 1] for Phase 1",
        })

    spec = _CONTRACT_SPECS.get(symbol, _CONTRACT_SPECS["MGC"])
    ibkr_action = "BUY" if direction == "long" else "SELL"

    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "4002"))
    client_id = int(os.environ.get("IBKR_EXEC_CLIENT_ID", "151"))

    try:
        import asyncio

        ib = IB()
        util.startLoop()

        async def _place() -> dict:
            await ib.connectAsync(host, port, clientId=client_id, timeout=15)
            try:
                cont = ContFuture(
                    symbol=symbol,
                    exchange=spec["exchange"],
                )
                qualified = await ib.qualifyContractsAsync(cont)
                if not qualified:
                    raise ValueError(f"Could not qualify continuous contract for {symbol}")
                cont = qualified[0]

                month = getattr(cont, "lastTradeDateOrContractMonth", "")
                if not month:
                    raise ValueError(f"Could not resolve front-month expiry for {symbol}")

                contract = Future(
                    symbol=symbol,
                    exchange=spec["exchange"],
                    currency=spec["currency"],
                    lastTradeDateOrContractMonth=month,
                )
                front = await ib.qualifyContractsAsync(contract)
                if not front:
                    raise ValueError(f"Could not qualify front-month contract for {symbol}")
                contract = front[0]

                order = MarketOrder(action=ibkr_action, totalQuantity=qty)
                trade = ib.placeOrder(contract, order)
                # Give it a moment to get an order ID
                await asyncio.sleep(1)

                order_id = trade.order.orderId
                status = trade.orderStatus.status or "Submitted"

                log.info("ibkr_executor.order_placed", extra={
                    "symbol": symbol,
                    "direction": direction,
                    "qty": qty,
                    "order_id": order_id,
                    "status": status,
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                })

                return {
                    "ibkr_order_id": order_id,
                    "status": "placed",
                    "message": (
                        f"{ibkr_action} {qty}x {symbol} paper order placed. "
                        f"OrderId={order_id} IBKRStatus={status}. "
                        f"Entry={entry:.4f} Stop={stop:.4f} Target={target:.4f}"
                    ),
                }
            finally:
                ib.disconnect()

        result = asyncio.run(_place())
        return json.dumps(result)

    except Exception as e:
        log.error("ibkr_executor.error", extra={"symbol": symbol, "error": str(e)}, exc_info=True)
        return json.dumps({
            "ibkr_order_id": -1,
            "status": "error",
            "message": str(e),
        })
