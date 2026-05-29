"""Regression: _reconcile_open_trades prefers real IBKR executions.

Before this change, the reconciler walked cached 1H bars and marked any open
trade closed at the *planned* stop_price / target_price as soon as the bar
touched either level. That recorded fictional fills (no slippage, no real
order interaction) into ``trades.exit_fill`` and biased every downstream
metric.

Now: open trades carry tp_order_id / sl_order_id from the bracket. On
reconcile we pull IBKR executions once per cycle, match by orderId, and
record ``exit_fill = execution.avgPrice`` with ``exit_reason='target_real'``
or ``'stop_real'``. If IBKR is unreachable OR no matching execution exists
yet, we fall back to the bar-touch logic and tag the reason with the
``_synthetic`` suffix so cost-aware reviewers can filter it.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest


@pytest.fixture
def temp_db(tmp_path):
    from src.db.models import init_db
    db = tmp_path / "judas_reconcile.db"
    init_db(str(db))
    return str(db)


def _insert_open_trade(db_path: str, *, tp_oid: str | None, sl_oid: str | None) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO trades
              (signal_id, strategy_id, strategy_family, strategy_version,
               ibkr_order_id, tp_order_id, sl_order_id,
               symbol, direction, qty, entry_fill, stop_price, target_price, status, opened_at)
            VALUES (NULL, 1, 'judas', 1, '1000', ?, ?,
                    'MGC', 'long', 1, 2000.0, 1990.0, 2020.0, 'open',
                    '2026-05-01T10:00:00Z')
            """,
            (tp_oid, sl_oid),
        )
        return int(cur.lastrowid)


def _bars_touching_stop() -> dict[str, pd.DataFrame]:
    rows = [
        {"ts": pd.Timestamp("2026-05-01T11:00:00Z"),
         "open": 2000.0, "high": 2005.0, "low": 1989.0, "close": 1992.0, "volume": 0},
    ]
    return {"MGC": pd.DataFrame(rows)}


def test_reconcile_uses_real_ibkr_fill_when_available(temp_db, monkeypatch):
    from src import portfolio_runtime as pr

    trade_id = _insert_open_trade(temp_db, tp_oid="1001", sl_oid="1002")

    # Fake IBKR execution: tp child order (1001) filled at 2019.75.
    fake_exec = SimpleNamespace(
        orderId=1001,
        avgPrice=2019.75,
        price=2019.75,
    )
    fake_fill = SimpleNamespace(
        execution=fake_exec,
        time=datetime(2026, 5, 1, 12, 30, 0, tzinfo=timezone.utc),
    )

    async def fake_fetch(*, host, port, client_id):
        return {1001: fake_fill}

    monkeypatch.setattr(pr, "_fetch_ibkr_fills_by_order_id", fake_fetch)

    closed = pr._reconcile_open_trades(
        temp_db, _bars_touching_stop(),
        host="x", port=4002, client_id=151,
    )
    assert closed == 1

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT exit_fill, exit_reason, status, pnl_dollars, closed_at "
            "FROM trades WHERE id=?", (trade_id,),
        ).fetchone()
    assert row[0] == pytest.approx(2019.75)
    assert row[1] == "target_real"
    assert row[2] == "closed"
    # entry=2000, exit=2019.75, dpp = tick_value/tick = 1.0/0.10 = 10, qty=1
    # pnl = (2019.75 - 2000) * 10 * 1 = 197.50
    assert row[3] == pytest.approx(197.50)
    assert "2026-05-01" in (row[4] or "")


def test_reconcile_falls_back_to_bar_touch_when_ibkr_unreachable(temp_db, monkeypatch):
    """When the IBKR fetch returns empty (connection failure, paper account
    session lost, or no executions for our child order ids), reconcile must
    still close trades via the bar-touch path so we don't silently leak open
    rows forever. The exit_reason is tagged ``stop_synthetic`` /
    ``target_synthetic`` so downstream code can filter them out."""
    from src import portfolio_runtime as pr

    trade_id = _insert_open_trade(temp_db, tp_oid="2001", sl_oid="2002")

    async def fake_fetch(*, host, port, client_id):
        return {}  # No fills available -- simulate unreachable IBKR

    monkeypatch.setattr(pr, "_fetch_ibkr_fills_by_order_id", fake_fetch)

    bars = _bars_touching_stop()  # low=1989 < stop=1990 -> stop hit
    closed = pr._reconcile_open_trades(
        temp_db, bars, host="x", port=4002, client_id=151,
    )
    assert closed == 1

    with sqlite3.connect(temp_db) as conn:
        row = conn.execute(
            "SELECT exit_fill, exit_reason, status FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
    assert row[0] == pytest.approx(1990.0)  # bar-touch uses stop_price level
    assert row[1] == "stop_synthetic"
    assert row[2] == "closed"


def test_compute_live_metrics_real_fills_only_filter(temp_db):
    """compute_live_metrics(real_fills_only=True) should exclude rows whose
    exit_reason ends in ``_synthetic``."""
    import json
    from src.research.live_review import compute_live_metrics

    with sqlite3.connect(temp_db) as conn:
        cur = conn.execute(
            "INSERT INTO active_strategies (symbol, strategy_family, version, params_json) "
            "VALUES ('MGC', 'judas', 1, ?)",
            (json.dumps({"strategy_name": "test_strat"}),),
        )
        sid = int(cur.lastrowid)
        # 6 closed trades: 3 real-fill wins, 3 synthetic losers
        for i, (pnl, reason) in enumerate([
            (50.0, "target_real"),
            (60.0, "target_real"),
            (70.0, "target_real"),
            (-30.0, "stop_synthetic"),
            (-40.0, "stop_synthetic"),
            (-50.0, "stop_synthetic"),
        ]):
            conn.execute(
                """INSERT INTO trades
                   (strategy_id, symbol, direction, qty, entry_fill, stop_price, target_price,
                    exit_fill, pnl_dollars, exit_reason, status, opened_at, closed_at)
                   VALUES (?, 'MGC', 'long', 1, 2000.0, 1990.0, 2020.0,
                           2010.0, ?, ?, 'closed',
                           ?, ?)""",
                (sid, pnl, reason,
                 f"2026-05-0{i+1}T10:00:00Z",
                 f"2026-05-0{i+1}T11:00:00Z"),
            )
        conn.commit()

    all_m = compute_live_metrics(db_path=temp_db, strategy_id=sid)
    real_m = compute_live_metrics(db_path=temp_db, strategy_id=sid, real_fills_only=True)

    assert all_m.n_closed_trades == 6
    assert real_m.n_closed_trades == 3
    # Real-fills cohort is 3 winners only -> total_realized_pnl == 180
    assert real_m.total_realized_pnl == pytest.approx(180.0)
