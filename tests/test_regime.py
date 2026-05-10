"""Tests for src.research.regime."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research.regime import tag_regime  # noqa: E402


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_trade(conn, *, symbol: str, pnl: float, when: datetime) -> None:
    conn.execute(
        """
        INSERT INTO trades
            (signal_id, strategy_id, symbol, direction, qty, entry_fill,
             pnl_dollars, status, opened_at, closed_at)
        VALUES (NULL, 1, ?, 'long', 1, 100.0, ?, 'closed', ?, ?)
        """,
        (symbol, float(pnl), _utc_iso(when), _utc_iso(when + timedelta(minutes=30))),
    )


def test_tag_regime_empty_db_returns_defaults(tmp_path):
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    tag = tag_regime(db_path=str(db))
    assert tag["vol_regime"] == "mid"
    assert tag["trend"] == "mixed"
    assert tag["leaders"] == []


def test_tag_regime_leaders_by_abs_pnl(tmp_path):
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    now = datetime.now(timezone.utc)
    with get_conn(str(db)) as conn:
        # MGC: large abs pnl, MNQ: medium, MCL: small, MET: tiny
        for i in range(5):
            _seed_trade(conn, symbol="MGC", pnl=200.0, when=now - timedelta(days=i))
            _seed_trade(conn, symbol="MNQ", pnl=-100.0, when=now - timedelta(days=i))
            _seed_trade(conn, symbol="MCL", pnl=10.0, when=now - timedelta(days=i))
            _seed_trade(conn, symbol="MET", pnl=1.0, when=now - timedelta(days=i))

    tag = tag_regime(db_path=str(db), lookback_days=10)
    assert tag["leaders"][:3] == ["MGC", "MNQ", "MCL"]


def test_tag_regime_trending_when_majority_up_days(tmp_path):
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    now = datetime.now(timezone.utc)
    with get_conn(str(db)) as conn:
        # 7 up days, 1 down day -> trending
        for i in range(7):
            _seed_trade(conn, symbol="MGC", pnl=50.0, when=now - timedelta(days=i))
        _seed_trade(conn, symbol="MGC", pnl=-30.0, when=now - timedelta(days=8))

    tag = tag_regime(db_path=str(db), lookback_days=15)
    assert tag["trend"] == "trending"


def test_tag_regime_returns_valid_vol_regime_label(tmp_path):
    db = tmp_path / "judas_crew.db"
    init_db(str(db))
    now = datetime.now(timezone.utc)
    with get_conn(str(db)) as conn:
        for i in range(60):
            _seed_trade(
                conn,
                symbol="MGC",
                pnl=(50.0 if i % 2 == 0 else -50.0),
                when=now - timedelta(days=i),
            )
    tag = tag_regime(db_path=str(db), lookback_days=30)
    assert tag["vol_regime"] in {"high", "mid", "low"}
