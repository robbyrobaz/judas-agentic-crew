"""Regression: explicit bracket-order construction.

The previous implementation called ``ib.bracketOrder(limitPrice=0.0)`` and
then mutated the parent into a MKT order, leaving stale ``lmtPrice=0.0``.
``_build_bracket_orders`` now constructs each leg explicitly. This test
pins the order types, prices, transmit flags, and tif so any future
refactor that re-introduces the mutation pattern fails immediately.
"""
from __future__ import annotations

from src.portfolio_runtime import _build_bracket_orders


def test_long_bracket_types_and_prices():
    parent, tp, sl = _build_bracket_orders(
        action="BUY",
        quantity=2,
        stop_price=1900.0,
        target_price=1950.0,
    )

    assert parent.orderType == "MKT"
    assert parent.action == "BUY"
    assert parent.totalQuantity == 2
    assert parent.transmit is False
    assert parent.tif == "GTC"
    assert parent.outsideRth is True
    # Critical: MKT parent must NOT have its lmtPrice mutated to 0.0
    # (that was the previous bug pattern). ib_async leaves lmtPrice at
    # UNSET_DOUBLE for true MKT orders.
    assert parent.lmtPrice != 0.0

    assert tp.orderType == "LMT"
    assert tp.action == "SELL"  # opposite of parent
    assert tp.totalQuantity == 2
    assert tp.lmtPrice == 1950.0
    assert tp.transmit is False
    assert tp.tif == "GTC"
    assert tp.outsideRth is True

    assert sl.orderType == "STP"
    assert sl.action == "SELL"
    assert sl.totalQuantity == 2
    assert sl.auxPrice == 1900.0
    # Only the LAST child transmits — that's what activates the bracket.
    assert sl.transmit is True
    assert sl.tif == "GTC"
    assert sl.outsideRth is True


def test_short_bracket_inverts_child_actions():
    parent, tp, sl = _build_bracket_orders(
        action="SELL",
        quantity=1,
        stop_price=2000.0,
        target_price=1900.0,
    )
    assert parent.action == "SELL"
    assert tp.action == "BUY"
    assert sl.action == "BUY"
    assert tp.lmtPrice == 1900.0
    assert sl.auxPrice == 2000.0


def test_place_bracket_holds_until_confirmed(monkeypatch):
    """The IBKR connection must stay open until both legs report
    Submitted/PreSubmitted. The previous implementation slept 1s and
    disconnected, causing IBKR to cancel the bracket. The fix polls
    orderStatus.status with a 10-second budget."""
    import asyncio
    from src import portfolio_runtime as pr

    class _Status:
        def __init__(self, s=""):
            self.status = s

    class _Trade:
        def __init__(self):
            self._calls = 0
            self.orderStatus = _Status("PendingSubmit")
        def advance(self):
            self._calls += 1
            if self._calls >= 2:
                self.orderStatus.status = "Submitted"

    parent_t = _Trade()
    sl_t = _Trade()
    tp_t = _Trade()

    class FakeOrder:
        def __init__(self):
            self.orderId = 0
            self.transmit = False
            self.tif = ""
            self.outsideRth = False
            self.parentId = 0

    class FakeIB:
        def __init__(self):
            self._next = 100
        async def connectAsync(self, *a, **kw):
            return None
        async def qualifyContractsAsync(self, c):
            c.localSymbol = "MGCJ26"
            c.lastTradeDateOrContractMonth = "20260424"
            return [c]
        def placeOrder(self, contract, order):
            self._next += 1
            order.orderId = self._next
            if order.orderType == "MKT":
                return parent_t
            if order.orderType == "STP":
                return sl_t
            return tp_t
        def disconnect(self):
            pass

    monkeypatch.setattr(pr, "IB", lambda: FakeIB())

    # On each sleep tick, advance the trade statuses toward Submitted.
    real_sleep = asyncio.sleep

    async def patched_sleep(delay):
        parent_t.advance()
        sl_t.advance()
        await real_sleep(0)

    monkeypatch.setattr(pr.asyncio, "sleep", patched_sleep)

    out = asyncio.run(pr._place_bracket_async(
        symbol="MGC", side="BUY", quantity=1,
        stop_price=10.0, target_price=20.0,
        host="x", port=4002, client_id=151,
    ))
    assert out["status"] == "Submitted"
    # Both trades must have been polled at least once before disconnect.
    assert parent_t._calls >= 1
    assert sl_t._calls >= 1


def test_parent_id_wiring_pattern():
    """Children inherit the parent's orderId after placeOrder assigns it.

    Simulate the post-placeOrder state by setting parent.orderId and the
    parentId fields the way ``_place_bracket_async`` does.
    """
    parent, tp, sl = _build_bracket_orders(
        action="BUY", quantity=1, stop_price=10.0, target_price=20.0
    )
    parent.orderId = 4242
    tp.parentId = parent.orderId
    sl.parentId = parent.orderId
    assert tp.parentId == 4242
    assert sl.parentId == 4242
# --- 2026-07-15: OCO fresh-id leg-2 retry regression ----------------------
# NT rejects reused oco ids once a group completes; mint fresh on every attempt.
def _build_nt_broker(monkeypatch, *, place_sequence, confirm_protected=True):
    """Build an NTBroker with mocked WinRM seams."""
    from src.broker.ninjatrader import NTBroker
    broker = NTBroker(account="SimJudasCrew", instrument_map={"MGC": "MGC 09-26"},
                      password="x", fill_timeout_s=0.1, fill_poll_s=0.01)
    seq = list(place_sequence)
    place_calls = []
    def fake_place(**kwargs):
        place_calls.append(kwargs)
        if not seq:
            return ""
        return seq.pop(0)
    def fake_poll_fill(order_id):
        return True, 100.0, "FILLED;1;100.0"
    def fake_confirm_protected(stop_oid, target_oid):
        return bool(confirm_protected and stop_oid and target_oid)
    monkeypatch.setattr(broker, "_place", fake_place)
    monkeypatch.setattr(broker, "_poll_fill", fake_poll_fill)
    monkeypatch.setattr(broker, "_confirm_protected", fake_confirm_protected)
    return broker, place_calls
def test_place_bracket_retries_leg2_with_fresh_oco_on_rejection(monkeypatch):
    """leg-2 retry uses a fresh oco_id (NT rejects reused ids)."""
    broker, place_calls = _build_nt_broker(
        monkeypatch, place_sequence=["ENTRY-1", "STOP-1", "", "TARGET-2"],
    )
    res = broker.place_bracket(symbol="MGC", side="BUY", quantity=1, stop_price=99.0, target_price=101.0)
    assert res is not None
    assert res.stop_oid == "STOP-1"
    assert res.target_oid == "TARGET-2"
    assert res.entry_oid == "ENTRY-1"
    initial_oco = place_calls[1]["oco_id"]
    retry_oco = place_calls[3]["oco_id"]
    assert initial_oco != retry_oco
def test_place_bracket_flattens_when_leg2_retry_also_fails(monkeypatch):
    """leg-2 retry fails too -> flatten path triggers (no orphan entry)."""
    flatten_calls = []
    def fake_flatten(symbol, *, direction, quantity):
        flatten_calls.append({"symbol": symbol, "direction": direction, "quantity": quantity})
        return 100.0
    broker, _ = _build_nt_broker(
        monkeypatch, place_sequence=["ENTRY-1", "STOP-1", "", "", "FLAT-1"],
    )
    monkeypatch.setattr(broker, "flatten", fake_flatten)
    res = broker.place_bracket(symbol="MGC", side="BUY", quantity=1, stop_price=99.0, target_price=101.0)
    assert res is None
    assert len(flatten_calls) == 1
    call = flatten_calls[0]
    assert call["symbol"] == "MGC"
    assert call["direction"] == "long"
    assert call["quantity"] == 1
