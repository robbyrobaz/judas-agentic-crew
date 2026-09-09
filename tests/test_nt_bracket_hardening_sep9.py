"""2026-09-09 bracket hardening (6J OCO-reuse cascade + reverse-position trap).

1. If the STOP leg is rejected, the target must NOT be placed on the same
   oco id (NT: 'The OCO ID cannot be reused') — go straight to the guard.
2. The naked guard must look at the book before flattening: a target that
   already filled means the book is flat, and a blind MARKET order would
   open a naked reverse position (2026-09-07 00:20 MST, -$550).
"""
from __future__ import annotations

import pytest


def _broker(monkeypatch, *, place_sequence, stop_alive=True,
            fills=None, position=None, close_ok=True):
    from src.broker.ninjatrader import NTBroker
    broker = NTBroker(account="SimJudasFutures", instrument_map={"6J": "6J 09-26"},
                      password="x", fill_timeout_s=0.1, fill_poll_s=0.01)
    seq = list(place_sequence)
    calls = {"place": [], "flatten": [], "close": [], "cancel": []}

    def fake_place(**kw):
        calls["place"].append(kw)
        return seq.pop(0) if seq else ""

    def fake_flatten(symbol, *, direction, quantity):
        calls["flatten"].append((symbol, direction, quantity))
        return 0.0065155

    def fake_close(instrument):
        calls["close"].append(instrument)
        return close_ok

    fills = fills or {}
    monkeypatch.setattr(broker, "_place", fake_place)
    monkeypatch.setattr(broker, "_poll_fill", lambda oid: (True, 0.006515, "FILLED;1;0.006515"))
    monkeypatch.setattr(broker, "_await_leg_live", lambda oid: stop_alive)
    monkeypatch.setattr(broker, "_confirm_protected", lambda s, t: bool(s and t))
    monkeypatch.setattr(broker, "check_fill",
                        lambda oid: (True, fills[oid], "FILLED") if oid in fills else (False, 0.0, "NOFILE"))
    monkeypatch.setattr(broker, "cancel", lambda oid, sym: calls["cancel"].append(oid) or True)
    monkeypatch.setattr(broker, "close_position_cmd", fake_close)
    monkeypatch.setattr(broker, "_read_position_file", lambda inst: position)
    monkeypatch.setattr(broker, "flatten", fake_flatten)
    return broker, calls


def _place_6j_short(broker):
    return broker.place_bracket(symbol="6J", side="SELL", quantity=1,
                                stop_price=0.0065213, target_price=0.0065133,
                                tick=0.0000005, entry_ref=0.0065183)


def test_stop_rejected_skips_target_on_same_oco(monkeypatch):
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP"],
                            stop_alive=False, position=("FLAT", 0, 0.0))
    res = _place_6j_short(broker)
    assert res is None
    types = [c["order_type"] for c in calls["place"]]
    assert types == ["MARKET", "STOPMARKET"], "no LIMIT may be placed on a dead OCO group"
    assert calls["close"] == ["6J 09-26"]      # engine-side close, then verified flat
    assert calls["flatten"] == []              # no blind MARKET order


def test_target_already_filled_means_no_flatten(monkeypatch):
    """Case B from the 6J post-mortem: target rounded through the market and
    filled instantly; stop cancelled by OCO. Book is flat -> guard must NOT
    send the MARKET flatten that opened the -$550 reverse short."""
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP", "TARGET"],
                            fills={"TARGET": 0.006511}, position=("FLAT", 0, 0.0))
    monkeypatch.setattr(broker, "_confirm_protected", lambda s, t: False)
    res = _place_6j_short(broker)
    assert res is None
    assert calls["flatten"] == []
    assert calls["close"] == []
    assert "STOP" in calls["cancel"]


def test_guard_flattens_real_book_when_close_cmd_fails(monkeypatch):
    """Legs dead, CLOSEPOSITION refused, NT file says we hold SHORT x1 ->
    MARKET flatten uses the side/qty NT reports."""
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP", "TARGET"],
                            position=("SHORT", 1, 0.006515), close_ok=False)
    monkeypatch.setattr(broker, "_confirm_protected", lambda s, t: False)
    res = _place_6j_short(broker)
    assert res is None
    assert calls["close"] == ["6J 09-26"]
    assert calls["flatten"] == [("6J", "short", 1)]


def test_guard_falls_back_to_assumed_flatten_when_book_unreadable(monkeypatch):
    """No fills, CLOSEPOSITION refused, position file unreadable -> legacy
    behaviour: flatten the assumed side/qty rather than leave a position."""
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP", "TARGET"],
                            position=None, close_ok=False)
    monkeypatch.setattr(broker, "_confirm_protected", lambda s, t: False)
    _place_6j_short(broker)
    assert calls["flatten"] == [("6J", "short", 1)]


def test_guard_close_ok_but_unverifiable_does_not_fire_blind_market(monkeypatch):
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP", "TARGET"],
                            position=None, close_ok=True)
    monkeypatch.setattr(broker, "_confirm_protected", lambda s, t: False)
    _place_6j_short(broker)
    assert calls["close"] == ["6J 09-26"]
    assert calls["flatten"] == []


def test_happy_path_unchanged(monkeypatch):
    broker, calls = _broker(monkeypatch, place_sequence=["ENTRY", "STOP", "TARGET"])
    res = _place_6j_short(broker)
    assert res is not None and res.target_oid == "TARGET"
    assert [c["order_type"] for c in calls["place"]] == ["MARKET", "STOPMARKET", "LIMIT"]
    assert calls["flatten"] == [] and calls["close"] == []
