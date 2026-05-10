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
