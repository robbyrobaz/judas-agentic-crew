"""Property tests for src.tools.cost_model."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.tools import cost_model  # noqa: E402

ACTIVE_SYMBOLS = ["MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"]


def test_commission_per_round_trip_is_2x_per_side():
    for sym in ACTIVE_SYMBOLS:
        rt = cost_model.commission_per_round_trip(sym)
        ps = cost_model.commission_per_side(sym)
        assert rt == pytest.approx(2.0 * ps)
        assert rt > 0


def test_commission_default_for_unknown_symbol():
    # Unknown symbol uses the pessimistic default.
    assert cost_model.commission_per_side("WTF") == pytest.approx(0.85)
    assert cost_model.commission_per_round_trip("WTF") == pytest.approx(1.70)


def test_slippage_dollars_symmetric_long_vs_short():
    # The function is direction-agnostic; same call returns same number.
    for sym in ACTIVE_SYMBOLS:
        s1 = cost_model.slippage_dollars(sym, "stop")
        s2 = cost_model.slippage_dollars(sym, "stop")
        assert s1 == s2


def test_slippage_stop_ge_target_for_known_symbols():
    # Stops gap; targets fill at the level or not at all.
    for sym in ACTIVE_SYMBOLS:
        stop_slip = cost_model.slippage_dollars(sym, "stop")
        tgt_slip = cost_model.slippage_dollars(sym, "target")
        assert stop_slip >= tgt_slip
        assert tgt_slip >= 0
        assert stop_slip >= 0


def test_round_trip_cost_monotonic_stop_vs_target():
    for sym in ACTIVE_SYMBOLS:
        stop_cost = cost_model.round_trip_cost(sym, "stop")
        target_cost = cost_model.round_trip_cost(sym, "target")
        assert stop_cost >= target_cost
        # Commission portion is always present.
        assert stop_cost > 0
        assert target_cost > 0


def test_round_trip_cost_default_is_pessimistic():
    # Default exit_reason should equal the stop case (pessimistic).
    for sym in ACTIVE_SYMBOLS:
        assert cost_model.round_trip_cost(sym) == cost_model.round_trip_cost(sym, "stop")


def test_round_trip_cost_unknown_exit_reason_is_entry_only_slippage():
    # An unknown reason (e.g. manual close, time-stop) → exit slip = 0
    # but commission still applies.
    for sym in ACTIVE_SYMBOLS:
        c = cost_model.round_trip_cost(sym, "manual_close")
        comm = cost_model.commission_per_round_trip(sym)
        entry_only_slip = cost_model.slippage_dollars(sym, "manual_close")
        assert c == pytest.approx(comm + entry_only_slip)


def test_apply_costs_to_trade_subtracts_cost():
    gross = 100.0
    net = cost_model.apply_costs_to_trade(
        symbol="MGC", gross_pnl=gross, exit_reason="stop", qty=1
    )
    expected = gross - cost_model.round_trip_cost("MGC", "stop")
    assert net == pytest.approx(expected)


def test_apply_costs_to_trade_scales_with_qty():
    cost_1 = cost_model.round_trip_cost("MNQ", "stop")
    net2 = cost_model.apply_costs_to_trade(
        symbol="MNQ", gross_pnl=0.0, exit_reason="stop", qty=2
    )
    assert net2 == pytest.approx(-2.0 * cost_1)


def test_apply_costs_floor_qty_to_one():
    # qty=0 still yields at least one round-trip charged.
    net = cost_model.apply_costs_to_trade(
        symbol="MGC", gross_pnl=0.0, exit_reason="stop", qty=0
    )
    assert net == pytest.approx(-cost_model.round_trip_cost("MGC", "stop"))


def test_cost_model_version_string():
    assert isinstance(cost_model.COST_MODEL_VERSION, str)
    assert len(cost_model.COST_MODEL_VERSION) > 0


def test_all_active_symbols_have_explicit_entries():
    for sym in ACTIVE_SYMBOLS:
        assert sym in cost_model.COMMISSION_PER_SIDE
        assert sym in cost_model.SLIPPAGE_TICKS_ENTRY
        assert sym in cost_model.SLIPPAGE_TICKS_STOP
        assert sym in cost_model.SLIPPAGE_TICKS_TARGET
