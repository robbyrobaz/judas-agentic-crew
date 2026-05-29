"""One-shot: attach stop-loss and take-profit to the open MET LONG position.

Shared paper account DUH860616 also holds workshop positions, so the NET MET
position on IBKR will appear as negative (short). This script ignores the
net and places 1-lot OCA SELL orders to exit our specific 1-lot LONG entry.

Orders placed:
  - SELL LimitOrder  @ 2368.5  (take profit, GTC)
  - SELL StopOrder   @ 2281.0  (stop loss, GTC)
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SYMBOL       = "MET"
EXCHANGE     = "CME"
QTY          = 1
STOP_PRICE   = 2281.0
TARGET_PRICE = 2368.5
OCA_GROUP    = "MET_trade2_brackets"

HOST         = "127.0.0.1"
PORT         = 4002
CLIENT_ID    = 151


async def _run() -> None:
    from ib_async import IB, ContFuture, Future, LimitOrder, StopOrder

    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    log.info("Connected to IBKR Gateway (clientId=%s)", CLIENT_ID)

    try:
        # Resolve front-month MET contract
        cont = ContFuture(SYMBOL, exchange=EXCHANGE)
        q = await ib.qualifyContractsAsync(cont)
        if not q:
            raise ValueError("Could not qualify ContFuture for MET")
        month = getattr(q[0], "lastTradeDateOrContractMonth", "")
        if not month:
            raise ValueError("Could not resolve front-month expiry for MET")
        log.info("Front month: %s", month)

        front = Future(SYMBOL, lastTradeDateOrContractMonth=month, exchange=EXCHANGE)
        fq = await ib.qualifyContractsAsync(front)
        if not fq:
            raise ValueError("Could not qualify front-month Future for MET")
        contract = fq[0]
        log.info("Contract: %s (conId=%s)", contract.localSymbol, contract.conId)

        # Get current price for sanity check
        ticker = ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(2)
        last = ticker.last or ticker.close or ticker.bid or None
        log.info("Current MET price: %s", last)
        if last is not None:
            if last >= TARGET_PRICE:
                log.warning(
                    "Current price %.2f >= target %.2f — take-profit already in the money. "
                    "Limit order will execute immediately at market. This closes the trade at a profit.",
                    last, TARGET_PRICE,
                )
            elif last <= STOP_PRICE:
                log.warning(
                    "Current price %.2f <= stop %.2f — stop already triggered. "
                    "Stop order may execute immediately. This closes the trade at a loss.",
                    last, STOP_PRICE,
                )
            else:
                log.info("Price %.2f is between stop %.2f and target %.2f — all good.", last, STOP_PRICE, TARGET_PRICE)

        # Check net position (informational only — account is shared with workshop)
        await ib.reqPositionsAsync()
        await asyncio.sleep(1)
        positions = ib.positions()
        met_pos = next((p for p in positions if p.contract.symbol == SYMBOL and p.position != 0), None)
        if met_pos is not None:
            log.info(
                "Net MET position on DUH860616: qty=%s avgCost=%s "
                "(includes workshop positions — we hold +1 lot inside this)",
                met_pos.position, met_pos.avgCost,
            )
        else:
            log.warning("No MET position found at all — trade may already be closed. Proceeding anyway.")

        # Take-profit limit order
        tp = LimitOrder("SELL", QTY, TARGET_PRICE)
        tp.tif = "GTC"
        tp.outsideRth = True
        tp.ocaGroup = OCA_GROUP
        tp.ocaType = 1  # cancel remaining on fill

        # Stop-loss stop order
        sl = StopOrder("SELL", QTY, STOP_PRICE)
        sl.tif = "GTC"
        sl.outsideRth = True
        sl.ocaGroup = OCA_GROUP
        sl.ocaType = 1

        tp_trade = ib.placeOrder(contract, tp)
        sl_trade = ib.placeOrder(contract, sl)
        log.info("Orders submitted — TP orderId=%s  SL orderId=%s", tp.orderId, sl.orderId)

        # Wait for acknowledgement
        deadline = asyncio.get_running_loop().time() + 10.0
        while True:
            tp_status = tp_trade.orderStatus.status or ""
            sl_status = sl_trade.orderStatus.status or ""
            log.info("TP status=%-15s  SL status=%s", tp_status or "(pending)", sl_status or "(pending)")
            confirmed = {"Submitted", "PreSubmitted", "Filled"}
            if tp_status in confirmed and sl_status in confirmed:
                break
            if asyncio.get_running_loop().time() >= deadline:
                log.warning("Timed out waiting for confirmation — orders may still be live on IBKR")
                break
            await asyncio.sleep(0.5)

        log.info(
            "DONE — TP orderId=%s @ %.2f | SL orderId=%s @ %.2f | OCA=%s",
            tp.orderId, TARGET_PRICE,
            sl.orderId, STOP_PRICE,
            OCA_GROUP,
        )

    finally:
        ib.disconnect()
        log.info("Disconnected")


if __name__ == "__main__":
    asyncio.run(_run())
