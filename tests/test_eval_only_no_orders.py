"""Per-strategy eval_only_no_orders override.

A strategy row whose params carry ``eval_only_no_orders=True`` must:
- Be evaluated (signal produced + recorded)
- NOT have any order placed at the broker
- Carry ``skip_reason='eval_only_no_orders'`` on its signal row

This is the operator's defensive mechanism for MCL ghost-containment
(operator tasks 1962/1963/1964/1966/1967/1968) \u2014 prior to this fix the
flag was silently ignored because ``place_orders`` was only honored at
the scan-level (main.py) and never re-read from per-strategy params.

This test pins both directions:
1. Flag set \u2192 no order placed, skip_reason='eval_only_no_orders'
2. Flag unset (or absent) \u2192 order IS placed normally
"""
from __future__ import annotations

import sqlite3
from typing import Any

import pytest


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "judas_crew_eval.db"
    from src.db.models import init_db

    init_db(str(db_path))
    return str(db_path)


def _fake_row(*, eval_only: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "execution_engine": "buffet_zoo",
        "strategy_type": "rsi",
        "symbol": "MGC",
        "qty": 1,
        "strategy_name": "fake_rsi",
    }
    if eval_only:
        params["eval_only_no_orders"] = True
    return {
        "id": 42,
        "symbol": "MGC",
        "strategy_family": "buffet_zoo",
        "version": 1,
        "params": params,
    }


def _fake_single_fire(active, bars_by_sym):
    from src.portfolio_runtime import ActiveFire

    return [
        ActiveFire(
            strategy_id=int(active["id"]),
            strategy_name=str(active["params"]["strategy_name"]),
            strategy_family="buffet_zoo",
            strategy_version=1,
            symbol="MGC",
            direction="long",
            entry=2000.0,
            stop=1990.0,
            target=2020.0,
            qty=1,
            rationale="fake rsi fire",
            features={"rsi": 25.0},
        )
    ]


def _install_mocks(monkeypatch, active_rows):
    from src import portfolio_runtime

    monkeypatch.setattr(portfolio_runtime, "init_db", lambda p: None)
    monkeypatch.setattr(
        portfolio_runtime,
        "list_active_strategies",
        lambda: active_rows,
    )
    monkeypatch.setattr(portfolio_runtime, "fetch_bars", lambda *a, **kw: {})
    monkeypatch.setattr(portfolio_runtime, "_is_instrument_blocked", lambda _s: False)
    monkeypatch.setattr(
        portfolio_runtime, "evaluate_active_strategy", _fake_single_fire
    )

    place_calls: list[dict[str, Any]] = []

    def fake_place(**kwargs):
        place_calls.append(kwargs)
        return {
            "parent_order_id": 9001,
            "tp_order_id": 9002,
            "sl_order_id": 9003,
            "local_symbol": kwargs.get("symbol", "MGC"),
            "status": "Submitted",
        }

    monkeypatch.setattr(portfolio_runtime, "place_bracket", fake_place)
    monkeypatch.setattr(portfolio_runtime, "place_bracket_nt", fake_place)
    return place_calls


def _signal_skip_reasons(db_path: str) -> list[str]:
    """skip_reason lives inside fire.features, JSON-serialized into agent_notes."""
    import json as _json

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT agent_notes FROM signals").fetchall()
    reasons = []
    for (notes,) in rows:
        if not notes:
            reasons.append(None)
            continue
        try:
            payload = _json.loads(notes)
        except (TypeError, ValueError):
            reasons.append(None)
            continue
        reasons.append(payload.get("skip_reason"))
    return reasons


def _signal_count(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])


def test_eval_only_flag_blocks_order(temp_db, monkeypatch):
    """Flag set on strategy params \u2192 no order placed, signal recorded."""
    row = _fake_row(eval_only=True)
    place_calls = _install_mocks(monkeypatch, [row])

    from src.portfolio_runtime import run_portfolio_scan

    result = run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=8, max_open_positions=6, max_trades_per_day=12,
        place_orders=True,  # scan-level ON, but per-strategy override blocks
    )

    assert place_calls == [], "no order should be placed when eval_only_no_orders=True"
    assert _signal_count(temp_db) == 1
    reasons = _signal_skip_reasons(temp_db)
    assert reasons == ["eval_only_no_orders"]
    decisions = [f["decision"] for f in result["fires"]]
    assert decisions == ["SKIP"]


def test_no_flag_allows_order(temp_db, monkeypatch):
    """Flag absent \u2192 order is placed normally."""
    row = _fake_row(eval_only=False)
    place_calls = _install_mocks(monkeypatch, [row])

    from src.portfolio_runtime import run_portfolio_scan

    result = run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=8, max_open_positions=6, max_trades_per_day=12,
        place_orders=True,
    )

    assert len(place_calls) == 1, "order should be placed normally"
    assert _signal_count(temp_db) == 1
    reasons = _signal_skip_reasons(temp_db)
    assert reasons == [None]
    decisions = [f["decision"] for f in result["fires"]]
    assert decisions == ["TRADE"]


def test_eval_only_flag_only_affects_flagged_strategy(temp_db, monkeypatch):
    """Mixed batch: only the flagged strategy is blocked; siblings trade."""
    flagged = _fake_row(eval_only=True)
    flagged["id"] = 100
    normal = _fake_row(eval_only=False)
    normal["id"] = 200
    place_calls = _install_mocks(monkeypatch, [flagged, normal])

    from src.portfolio_runtime import run_portfolio_scan

    run_portfolio_scan(
        db_path=temp_db,
        host="x", port=4002, data_client_id=150, exec_client_id=151,
        max_new_trades=8, max_open_positions=6, max_trades_per_day=12,
        place_orders=True,
    )

    assert len(place_calls) == 1
    assert place_calls[0]["symbol"] == "MGC"  # from normal row
    # Two signals: one with skip_reason='eval_only_no_orders', one without
    import json as _json

    with sqlite3.connect(temp_db) as conn:
        rows = conn.execute(
            "SELECT strategy_id, agent_notes FROM signals ORDER BY id"
        ).fetchall()
    by_sid = {}
    for sid, notes in rows:
        try:
            payload = _json.loads(notes) if notes else {}
        except (TypeError, ValueError):
            payload = {}
        by_sid[sid] = payload.get("skip_reason")
    assert by_sid[100] == "eval_only_no_orders"
    assert by_sid[200] is None
