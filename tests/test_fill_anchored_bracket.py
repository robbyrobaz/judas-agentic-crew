"""Fill-anchored bracket legs (2026-07-20).

Live failure 2026-07-20 02:21Z: MET long signaled ~1877 with stop 1875 /
target 1886; the MARKET entry filled at 1873 (slippage past the stop). The
protective SELL stop then sat ABOVE the long fill → NT rejected it → the OCO
group died → the target's 'OCO ID cannot be reused' rejection → NAKED_RISK
emergency flatten (−2pts). Fix: re-anchor stop/target DISTANCES to the actual
fill via entry_ref, plus a wrong-side clamp as last resort.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.broker.ninjatrader import NTBroker


def _broker(monkeypatch, fill_price: float):
    b = NTBroker(account="SimTest", instrument_map={"MET": "MET 07-26"},
                 password="x")
    placed = []

    def fake_place(*, action, qty, order_type, limit_price, stop_price,
                   oco_id, instrument):
        placed.append({"action": action, "type": order_type,
                       "limit": limit_price, "stop": stop_price, "oco": oco_id})
        return f"OID{len(placed)}"

    monkeypatch.setattr(b, "_place", fake_place)
    monkeypatch.setattr(b, "_poll_fill", lambda oid: (True, fill_price, "FILLED"))
    monkeypatch.setattr(b, "_confirm_protected", lambda s, t: True)
    return b, placed


def test_met_slippage_case_reanchors_to_fill(monkeypatch):
    """The exact live failure: signal 1877, stop 1875, target 1886, fill 1873.
    With entry_ref the bracket re-anchors: stop 1871 (2 below fill), target
    1882 (9 above fill) — both on the correct side."""
    b, placed = _broker(monkeypatch, fill_price=1873.0)
    res = b.place_bracket(symbol="MET", side="BUY", quantity=1,
                          stop_price=1875.0, target_price=1886.0,
                          tick=0.5, entry_ref=1877.0)
    assert res is not None
    stop = next(p for p in placed if p["type"] == "STOPMARKET")
    target = next(p for p in placed if p["type"] == "LIMIT")
    assert stop["stop"] == 1871.0     # fill 1873 − dist 2
    assert target["limit"] == 1882.0  # fill 1873 + dist 9
    assert stop["stop"] < 1873.0 < target["limit"]


def test_short_side_reanchors_mirrored(monkeypatch):
    b, placed = _broker(monkeypatch, fill_price=1880.0)
    res = b.place_bracket(symbol="MET", side="SELL", quantity=1,
                          stop_price=1879.0, target_price=1868.0,
                          tick=0.5, entry_ref=1877.0)  # stop 2 above, target 9 below
    assert res is not None
    stop = next(p for p in placed if p["type"] == "STOPMARKET")
    target = next(p for p in placed if p["type"] == "LIMIT")
    assert stop["stop"] == 1882.0     # fill 1880 + 2
    assert target["limit"] == 1871.0  # fill 1880 − 9
    assert target["limit"] < 1880.0 < stop["stop"]


def test_no_entry_ref_wrong_side_stop_clamped(monkeypatch):
    """Without entry_ref (legacy callers), a wrong-side stop is clamped one
    tick past the fill instead of being submitted DOA."""
    b, placed = _broker(monkeypatch, fill_price=1873.0)
    res = b.place_bracket(symbol="MET", side="BUY", quantity=1,
                          stop_price=1875.0, target_price=1886.0, tick=0.5)
    assert res is not None
    stop = next(p for p in placed if p["type"] == "STOPMARKET")
    assert stop["stop"] < 1873.0  # clamped below the fill, not 1875


def test_normal_fill_unchanged_without_ref(monkeypatch):
    """No slippage, no entry_ref → caller's absolute prices pass through."""
    b, placed = _broker(monkeypatch, fill_price=1877.0)
    res = b.place_bracket(symbol="MET", side="BUY", quantity=1,
                          stop_price=1875.0, target_price=1886.0, tick=0.5)
    assert res is not None
    stop = next(p for p in placed if p["type"] == "STOPMARKET")
    target = next(p for p in placed if p["type"] == "LIMIT")
    assert stop["stop"] == 1875.0
    assert target["limit"] == 1886.0
