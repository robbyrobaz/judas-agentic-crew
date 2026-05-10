"""Regression: pair legs are atomic across IBKR + DB.

Previously, ``run_portfolio_scan`` walked the active strategies and for
each pair-engine row placed leg A then leg B without coupling them. If
leg B failed to gate (max_open_positions, max_trades_per_day) or its
``place_bracket`` call raised, leg A remained live both at IBKR and in
the local DB -- a naked single-leg pair.

Fix: when a pair row produces two ``ActiveFire``s and leg A trades but
leg B does not, cancel leg A's IBKR parent order via ``cancel_order``
and DELETE leg A's signal+trade rows. This test simulates leg B failing
under three conditions and asserts the cancel is issued and the DB is
clean.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Any

import pytest


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "judas_crew_pair.db"
    from src.db.models import init_db

    init_db(str(db_path))
    return str(db_path)


def _fake_pair_row() -> dict[str, Any]:
    return {
        "id": 7,
        "symbol": "MGC",  # ignored for pair
        "strategy_family": "buffet_pair",
        "version": 1,
        "params": {
            "execution_engine": "buffet_pair",
            "strategy_name": "test_pair_alpha",
            "qty": 1,
        },
    }


def _fake_pair_fires(active, bars_by_sym):
    from src.portfolio_runtime import ActiveFire

    common = dict(
        strategy_id=int(active["id"]),
        strategy_name=str(active["params"]["strategy_name"]),
        strategy_family="buffet_pair",
        strategy_version=1,
        qty=1,
        rationale="pair leg fired",
        features={"pair_id": "test_pair_alpha"},
    )
    return [
        ActiveFire(symbol="MGC", direction="long", entry=2000.0, stop=1990.0,
                   target=2020.0, **common),
        ActiveFire(symbol="MNQ", direction="short", entry=18000.0, stop=18050.0,
                   target=17900.0, **common),
    ]


def _install_pair_mocks(monkeypatch, *, leg_b_fails: bool, leg_b_raises: bool = False):
    """Patch portfolio_runtime so the test can drive run_portfolio_scan
    deterministically without IBKR or workshop parquet."""
    from src import portfolio_runtime

    monkeypatch.setattr(portfolio_runtime, "init_db", lambda p: None)
    monkeypatch.setattr(
        portfolio_runtime,
        "list_active_strategies",
        lambda: [_fake_pair_row()],
    )
    monkeypatch.setattr(portfolio_runtime, "fetch_bars", lambda *a, **kw: {})
    monkeypatch.setattr(portfolio_runtime, "evaluate_active_strategy", _fake_pair_fires)

    cancel_calls: list[int] = []

    def fake_cancel(*, parent_order_id, host, port, client_id):
        cancel_calls.append(int(parent_order_id))

    monkeypatch.setattr(portfolio_runtime, "cancel_order", fake_cancel)

    place_calls: list[dict[str, Any]] = []
    next_order_id = {"id": 1000}

    def fake_place(**kwargs):
        place_calls.append(kwargs)
        next_order_id["id"] += 1
        sym = kwargs["symbol"]
        if sym == "MNQ":
            if leg_b_raises:
                raise RuntimeError("simulated IBKR rejection on leg B")
            if leg_b_fails:
                # Should never be reached if leg_b_fails is driven via
                # gating; use this branch only when raising.
                raise RuntimeError("unreachable")
        return {
            "parent_order_id": next_order_id["id"],
            "tp_order_id": next_order_id["id"] + 1000,
            "sl_order_id": next_order_id["id"] + 2000,
            "local_symbol": sym,
            "status": "Submitted",
        }

    monkeypatch.setattr(portfolio_runtime, "place_bracket", fake_place)
    return cancel_calls, place_calls


def _count_db(db_path: str) -> tuple[int, int]:
    with sqlite3.connect(db_path) as conn:
        s = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    return int(s), int(t)


def test_pair_leg_b_raises_rolls_back_leg_a(temp_db, monkeypatch):
    cancel_calls, _ = _install_pair_mocks(monkeypatch, leg_b_fails=False, leg_b_raises=True)
    from src.portfolio_runtime import run_portfolio_scan

    result = run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=8, max_open_positions=6, max_trades_per_day=12,
        place_orders=True,
    )

    decisions = [f["decision"] for f in result["fires"]]
    assert decisions == ["ROLLED_BACK", "SKIP"]

    # Cancel was called exactly once for leg A's parent order id.
    assert len(cancel_calls) == 1
    assert cancel_calls[0] == 1001  # first place call increments to 1001

    # DB is clean: leg A's signal + trade rows were deleted; only leg B's
    # SKIP signal remains.
    sig_count, trade_count = _count_db(temp_db)
    assert sig_count == 1, f"expected only the leg B SKIP signal, got {sig_count}"
    assert trade_count == 0, f"expected no trades, got {trade_count}"


def test_pair_leg_b_gated_rolls_back_leg_a(temp_db, monkeypatch):
    """If leg B is gated (e.g. max_open_positions hit) leg A still rolls back."""
    cancel_calls, _ = _install_pair_mocks(monkeypatch, leg_b_fails=False)
    from src.portfolio_runtime import run_portfolio_scan

    # Force leg B to be gated by setting max_new_trades=1 -> after leg A
    # places, placed=1 so leg B is below the cap path BUT we also set
    # max_new_trades to 1 so leg B falls through to SKIP because
    # placed_so_far already reached max_new_trades.
    result = run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=1,  # <-- only one trade allowed
        max_open_positions=6, max_trades_per_day=12,
        place_orders=True,
    )

    decisions = [f["decision"] for f in result["fires"]]
    assert decisions == ["ROLLED_BACK", "SKIP"]
    assert len(cancel_calls) == 1

    sig_count, trade_count = _count_db(temp_db)
    assert sig_count == 1
    assert trade_count == 0


def test_pair_both_legs_succeed_no_rollback(temp_db, monkeypatch):
    cancel_calls, _ = _install_pair_mocks(monkeypatch, leg_b_fails=False)
    from src.portfolio_runtime import run_portfolio_scan

    result = run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=8, max_open_positions=6, max_trades_per_day=12,
        place_orders=True,
    )
    decisions = [f["decision"] for f in result["fires"]]
    assert decisions == ["TRADE", "TRADE"]
    assert cancel_calls == []
    sig_count, trade_count = _count_db(temp_db)
    assert sig_count == 2
    assert trade_count == 2
