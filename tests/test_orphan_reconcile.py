"""Regression: orphan NT positions (no matching DB trade) get auto-flattened.

Before this fix, NT positions opened by orphan OCO legs accumulated past
NT's max-position cap because nothing in the scan caught the case where NT
shows a position but the DB has no open trade row for it. Three live
emergencies (2026-07-09/10/13) all stem from this gap.

The fix: a STANDING NT-TRUTH RECONCILE in run_portfolio_scan that runs
after _reconcile_nt_fills() (only on route='ninjatrader'). It calls
src.research.agent_tools.get_nt_positions() (the broker's own truth),
loads DB open trades, and flattens any NT position whose
(symbol, direction) has no matching DB row AND whose symbol resolves
to a live NT contract (skipping expired contracts).

This test stubs the truth source and the broker so the flatten path is
exercised end-to-end with no IBKR/NT network dependency.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def temp_db(tmp_path):
    from src.db.models import init_db
    db = tmp_path / "judas_orphan.db"
    init_db(str(db))
    return str(db)


def _insert_open_trade(db_path: str) -> int:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades
              (signal_id, strategy_id, strategy_family, strategy_version,
               ibkr_order_id, tp_order_id, sl_order_id,
               symbol, direction, qty, entry_fill, stop_price, target_price,
               status, opened_at)
            VALUES (NULL, 1, 'judas', 1, '1000', '9001', '9002',
                    'MGC', 'long', 1, 2000.0, 1990.0, 2020.0,
                    'open', '2026-05-01T10:00:00Z')
            """,
        )
        return int(cur.lastrowid)


def _fake_nt_truth(positions):
    return {"ok": True, "account": "SimJudasCrew",
            "open_positions": list(positions), "flat": not positions}


class _FakeBroker:
    def __init__(self, *a, **kw):
        pass

    def flatten(self, symbol, *, direction, quantity):
        return 2000.0


def test_orphan_nt_position_gets_flattened(temp_db, monkeypatch):
    from src import portfolio_runtime as pr

    nt_truth = _fake_nt_truth([{
        "instrument": "MGC", "position": 1, "side": "LONG", "qty": 1,
        "last_exec_price": 2000.0, "last_exec_utc": "2026-07-12T00:00:00Z",
    }])

    state = {"calls": []}

    class _B:
        def __init__(self, *a, **kw):
            pass
        def flatten(self, symbol, *, direction, quantity):
            state["calls"].append({
                "symbol": symbol, "direction": direction, "quantity": quantity,
            })
            return 2000.0

    monkeypatch.setattr(pr, "_nt_broker", lambda: _B())
    monkeypatch.setattr(pr, "get_nt_positions",
                        lambda *, account="SimJudasCrew": nt_truth)
    monkeypatch.setattr(pr, "_resolve_nt_instrument_map",
                        lambda cfg_map: {"MGC": "MGC 09-26"})

    closed = pr._reconcile_nt_orphans(temp_db)
    assert closed == 1
    assert len(state["calls"]) == 1
    call = state["calls"][0]
    assert call["symbol"] == "MGC"
    assert call["direction"] == "long"
    assert call["quantity"] == 1


def test_matched_nt_position_is_left_alone(temp_db, monkeypatch):
    from src import portfolio_runtime as pr

    _insert_open_trade(temp_db)

    nt_truth = _fake_nt_truth([{
        "instrument": "MGC", "position": 1, "side": "LONG", "qty": 1,
        "last_exec_price": 2000.0, "last_exec_utc": "2026-07-12T00:00:00Z",
    }])

    state = {"calls": []}

    class _B:
        def __init__(self, *a, **kw):
            pass
        def flatten(self, symbol, *, direction, quantity):
            state["calls"].append({"symbol": symbol})  # shorter x
