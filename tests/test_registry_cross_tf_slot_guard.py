"""Cross-timeframe same-csid supersede guard.

The `custom` supersede slot key is the custom_strategy_id ALONE
(`_slot_key_for` -> "csid:<N>"), so promoting a second "port" of one csid at a
different timeframe silently supersedes the already-active row. Observed
2026-10-20: #4990 (MNQ csid 434, 15m) died 5s after promotion to make room for
##4991 (same csid, 5m). The guard helper shipped UN-CALLED (dead code) until
2026-10-21; these tests pin the wiring so it cannot silently regress to a
decorator again.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "registry.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(path))
    from src.db.models import init_db

    init_db(path)
    return str(path)


def _seed_custom(db_path, *, symbol, family, csid, timeframe):
    """Insert an ACTIVE custom row directly (no guard on the write path)."""
    params = {
        "symbol": symbol,
        "strategy_family": family,
        "execution_engine": "custom",
        "custom_strategy_id": str(csid),
        "timeframe": timeframe,
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        # _validate_custom_link runs first, so the code link must be live.
        conn.execute(
            """
            INSERT INTO custom_strategies
                (id, created_at_utc, name, symbol, code, rationale,
                 backtest_metrics_json, active, retired_at_utc)
            VALUES (?, '2026-10-01T00:00:00Z', ?, ?,
                    'def evaluate(bars, params):\n    return {}',
                    'test', '{}', 1, NULL)
            """,
            (csid, f"csid{csid}_{symbol.lower()}", symbol),
        )
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json, state, notes)
            VALUES (?, ?, 1, ?, '{"profit_factor": "2.0", "n_trades": "50"}',
                    'active', 'seed')
            """,
            (symbol, family, json.dumps(params)),
        )
        conn.commit()
        return int(cur.lastrowid)


def _make_candidate(db_path, *, params, symbol, family):
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.execute(
            """
            INSERT INTO strategy_candidates
                (ts_utc, symbol, strategy_family, source_experiment_id, params_json,
                 metrics_json, decision, rationale, status)
            VALUES (?, ?, ?, NULL, ?, ?, 'promote', 'test', 'candidate')
            """,
            ("2026-10-21T00:00:00Z", symbol, family, json.dumps(params),
             json.dumps({"profit_factor": 2.0, "n_trades": 50})),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_promote_refuses_cross_timeframe_same_csid_swap(db_path):
    """The #4990 -> #4991 shape: live 15m, candidate 5m, same csid."""
    _seed_custom(db_path, symbol="MNQ", family="custom_silver_bullet",
                 csid=434, timeframe="15m")
    cid = _make_candidate(
        db_path,
        symbol="MNQ",
        family="custom_silver_bullet",
        params={
            "symbol": "MNQ", "strategy_family": "custom_silver_bullet",
            "execution_engine": "custom", "custom_strategy_id": "434",
            "timeframe": "5m",
        },
    )
    from src.strategy_registry import promote_candidate

    with pytest.raises(ValueError, match="ALREADY LIVE"):
        promote_candidate(cid)

    # And nothing was mutated: the 15m row must still be active.
    with sqlite3.connect(db_path) as conn:
        states = conn.execute("SELECT state FROM active_strategies").fetchall()
        cstate = conn.execute(
            "SELECT status FROM strategy_candidates WHERE id = ?", (cid,)
        ).fetchone()
    assert [r[0] for r in states] == ["active"]
    assert cstate[0] == "candidate"


def test_promote_allows_same_timeframe_same_csid_repromotion(db_path):
    """Same slot, same tf -> the legitimate v+1 re-promotion path is untouched."""
    _seed_custom(db_path, symbol="MGC", family="custom_sb", csid=270,
                 timeframe="5m")
    cid = _make_candidate(
        db_path,
        symbol="MGC",
        family="custom_sb",
        params={
            "symbol": "MGC", "strategy_family": "custom_sb",
            "execution_engine": "custom", "custom_strategy_id": "270",
            "timeframe": "5m",
        },
    )
    from src.strategy_registry import promote_candidate

    row = promote_candidate(cid, notes="v+1 bump")
    with sqlite3.connect(db_path) as conn:
        states = sorted(r[0] for r in conn.execute(
            "SELECT state FROM active_strategies").fetchall())
    assert row.version == 2
    assert states == ["active", "superseded"]


def test_promote_allows_different_csid_same_family(db_path):
    """Different code = different slot; cross-csid coexistence must survive."""
    _seed_custom(db_path, symbol="MGC", family="custom_sb", csid=270,
                 timeframe="5m")
    _seed_custom(db_path, symbol="MGC", family="custom_sb", csid=999,
                 timeframe="5m")
    with sqlite3.connect(db_path) as conn:  # drop the placeholder 999 row
        conn.execute("DELETE FROM active_strategies WHERE CAST(JSON_EXTRACT("
                     "params_json,'$.custom_strategy_id') AS INTEGER)=999")
        conn.commit()
    cid = _make_candidate(
        db_path,
        symbol="MGC",
        family="custom_sb",
        params={
            "symbol": "MGC", "strategy_family": "custom_sb",
            "execution_engine": "custom", "custom_strategy_id": "999",
            "timeframe": "5m",
        },
    )
    from src.strategy_registry import promote_candidate

    promote_candidate(cid)
    with sqlite3.connect(db_path) as conn:
        states = sorted(r[0] for r in conn.execute(
            "SELECT state FROM active_strategies").fetchall())
    assert states == ["active", "active"]


def test_insert_active_strategy_also_guarded(db_path):
    """A direct insert must not be a bypass around the promote-path guard."""
    _seed_custom(db_path, symbol="MNQ", family="custom_sb", csid=434,
                 timeframe="15m")
    from src.strategy_registry import insert_active_strategy

    with pytest.raises(ValueError, match="ALREADY LIVE"):
        insert_active_strategy(
            symbol="MNQ", strategy_family="custom_sb",
            params={
                "symbol": "MNQ", "strategy_family": "custom_sb",
                "execution_engine": "custom", "custom_strategy_id": "434",
                "timeframe": "5m",
            },
        )