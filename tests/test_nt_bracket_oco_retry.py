"""OCO fresh-id leg-2 retry regression (2026-07-15).

Root-cause guard for the orphan-entry cluster: NTBroker.place_bracket used
to mint one oco_id and reuse it on any retry. NT rejects reused ids once a
group completes, leaving leg-2 unplaced -> entry unprotected -> orphan.

This test module lives separately from test_bracket_orders.py so it can be
collected without the pre-existing IndentationError in
src/portfolio_runtime.py (the orphan-reconcile work that landed
incompletely). Tests here exercise ONLY src.broker.ninjatrader.
"""
from __future__ import annotations


def _build_nt_broker(monkeypatch, *, place_sequence, confirm_protected=True):
    """Build an NTBroker with mocked WinRM seams (no network)."""
    from src.broker.ninjatrader import NTBroker

    broker = NTBroker(
        account="SimJudasCrew",
        instrument_map={"MGC": "MGC 09-26"},
        password="x",  # bypass WINDOWS_PASSWORD env lookup
        fill_timeout_s=0.1,
        fill_poll_s=0.01,
    )
    seq = list(place_sequence)
    place_calls = []

    def fake_place(**kwargs):
        place_calls.append(kwargs)
        if not seq:
            return ""
        return seq.pop(0)

    def fake_poll_fill(order_id):
        # Instant entry fill at $100.00 — price doesn't matter for these tests.
        return True, 100.0, "FILLED;1;100.0"

    def fake_confirm_protected(stop_oid, target_oid):
        return bool(confirm_protected and stop_oid and target_oid)

    monkeypatch.setattr(broker, "_place", fake_place)
    monkeypatch.setattr(broker, "_poll_fill", fake_poll_fill)
    monkeypatch.setattr(broker, "_confirm_protected", fake_confirm_protected)
    return broker, place_calls


def test_place_bracket_retries_leg2_with_fresh_oco_on_rejection(monkeypatch):
    """leg-2 (target) was rejected on the first attempt while leg-1 (stop)
    is live. The broker must retry leg-2 ONCE with a FRESH oco_id (new uuid
    suffix), NOT the same id — NT rejects reused oco ids once a group
    completes. Successful retry -> both legs live -> trade is protected.
    """
    broker, place_calls = _build_nt_broker(
        monkeypatch,
        # entry OK, stop OK, target REJECTED on first try, target OK on retry
        place_sequence=["ENTRY-1", "STOP-1", "", "TARGET-2"],
    )
    res = broker.place_bracket(
        symbol="MGC", side="BUY", quantity=1,
        stop_price=99.0, target_price=101.0,
    )
    assert res is not None, "leg-2 retry succeeded — result must not be None"
    assert res.stop_oid == "STOP-1"
    assert res.target_oid == "TARGET-2"
    assert res.entry_oid == "ENTRY-1"

    # Calls: [0]=entry(MARKET, no oco), [1]=stop, [2]=target-rejected,
    # [3]=target-retry. The retry MUST use a fresh oco_id, not the same.
    initial_oco = place_calls[1]["oco_id"]
    retry_oco = place_calls[3]["oco_id"]
    assert initial_oco, "first stop placement must carry an oco_id"
    assert retry_oco, "retry target placement must carry an oco_id"
    assert initial_oco != retry_oco, (
        "leg-2 retry must mint a FRESH oco_id (NT rejects reused ids once "
        "a group completes) — same id => orphan entry on next failure"
    )


def test_place_bracket_flattens_when_leg2_retry_also_fails(monkeypatch):
    """If the leg-2 retry also rejects (different oco id, NT still refuses),
    the broker must NOT leave the entry unprotected. It must flatten the
    position via the existing naked-position guard path.
    """
    flatten_calls = []

    def fake_flatten(symbol, *, direction, quantity):
        flatten_calls.append({"symbol": symbol, "direction": direction, "quantity": quantity})
        return 100.0
    broker, _place_calls = _build_nt_broker(
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
