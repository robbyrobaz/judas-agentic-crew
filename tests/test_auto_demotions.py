"""Phase 2 demotion-rollback tests.

Covers retire_strategy + reactivate_demoted + dashboard endpoints.
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


def _seed_active(symbol: str = "MGC", family: str = "judas_1h", params: dict | None = None) -> int:
    from src.strategy_registry import activate_seed_strategy

    return activate_seed_strategy(
        symbol=symbol,
        strategy_family=family,
        params=params or {"symbol": symbol, "strategy_family": family, "tag": "v1"},
        metrics={"seeded": True},
        notes="test seed",
    )


def test_retire_strategy_inserts_demotion_row(db_path):
    from src.strategy_registry import retire_strategy

    sid = _seed_active(symbol="AA", family="fam")
    metrics = {"pf_20": 0.4, "max_consec_losers": 7}
    demotion_id = retire_strategy(
        strategy_id=sid, reason="pf_20 below threshold", metrics_snapshot=metrics
    )
    assert demotion_id > 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM auto_demotions WHERE id = ?", (demotion_id,)
        ).fetchone()
        assert row is not None
        assert row["strategy_id"] == sid
        assert row["symbol"] == "AA"
        assert row["strategy_family"] == "fam"
        assert row["reason"] == "pf_20 below threshold"
        assert json.loads(row["metrics_snapshot_json"]) == metrics
        assert json.loads(row["params_json"])["symbol"] == "AA"
        assert row["reactivated_at_utc"] is None

        active = conn.execute(
            "SELECT state FROM active_strategies WHERE id = ?", (sid,)
        ).fetchone()
        assert active["state"] == "retired"


def test_retire_strategy_atomic_on_failure(db_path, monkeypatch):
    """If the auto_demotions INSERT fails mid-tx, active_strategies row stays active."""
    from src import strategy_registry

    sid = _seed_active(symbol="BB", family="fam2")

    real_get_conn = strategy_registry.get_conn

    class FailingConn:
        def __init__(self, inner):
            self._inner = inner
            self._call_count = 0

        def execute(self, sql, *args, **kwargs):
            # Fail specifically on the auto_demotions INSERT.
            if "INSERT INTO auto_demotions" in sql:
                raise sqlite3.OperationalError("simulated insert failure")
            return self._inner.execute(sql, *args, **kwargs)

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    from contextlib import contextmanager

    @contextmanager
    def fake_get_conn(path):
        with real_get_conn(path) as inner:
            yield FailingConn(inner)

    monkeypatch.setattr(strategy_registry, "get_conn", fake_get_conn)

    with pytest.raises(sqlite3.OperationalError):
        strategy_registry.retire_strategy(
            strategy_id=sid, reason="x", metrics_snapshot={"pf": 0.1}
        )

    # Verify active row was NOT marked retired.
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT state FROM active_strategies WHERE id = ?", (sid,)
        ).fetchone()
        assert row["state"] == "active"
        # And no auto_demotions row was committed.
        count = conn.execute(
            "SELECT COUNT(*) FROM auto_demotions WHERE strategy_id = ?", (sid,)
        ).fetchone()[0]
        assert count == 0


def test_reactivate_demoted_round_trip(db_path):
    from src.strategy_registry import (
        reactivate_demoted,
        retire_strategy,
    )

    params = {
        "symbol": "CC",
        "strategy_family": "fam3",
        "strategy_name": "round_trip",
        "target_r": 2.5,
    }
    sid = _seed_active(symbol="CC", family="fam3", params=params)
    demotion_id = retire_strategy(
        strategy_id=sid, reason="false demotion", metrics_snapshot={"pf_20": 0.5}
    )

    new_sid = reactivate_demoted(demotion_id=demotion_id)
    assert new_sid > 0
    assert new_sid != sid

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        new_row = conn.execute(
            "SELECT * FROM active_strategies WHERE id = ?", (new_sid,)
        ).fetchone()
        assert new_row["state"] == "active"
        assert json.loads(new_row["params_json"])["target_r"] == 2.5
        assert new_row["version"] == 2  # seed was version 1
        assert "Reactivated from demotion" in (new_row["notes"] or "")

        demo_row = conn.execute(
            "SELECT * FROM auto_demotions WHERE id = ?", (demotion_id,)
        ).fetchone()
        assert demo_row["reactivated_at_utc"] is not None
        assert demo_row["reactivated_strategy_id"] == new_sid


def test_reactivate_demoted_twice_raises(db_path):
    from src.strategy_registry import reactivate_demoted, retire_strategy

    sid = _seed_active(symbol="DD", family="fam4")
    demotion_id = retire_strategy(
        strategy_id=sid, reason="x", metrics_snapshot={}
    )
    reactivate_demoted(demotion_id=demotion_id)
    with pytest.raises(ValueError):
        reactivate_demoted(demotion_id=demotion_id)


def test_dashboard_demotions_endpoints(db_path, monkeypatch, tmp_path):
    pytest.importorskip("flask")
    # Make sure the dashboard's _db_path() points at our isolated DB.
    monkeypatch.setenv("JUDAS_DB_PATH", db_path)

    # Patch load_config so dashboard's _db_path resolves to our test DB.
    from src.dashboard import app as dash_app

    _resolved_db = str(db_path)

    class _Cfg:
        pass

    _Cfg.db_path = _resolved_db
    monkeypatch.setattr(dash_app, "load_config", lambda: _Cfg())

    from src.strategy_registry import retire_strategy

    sid = _seed_active(symbol="EE", family="fam5")
    demotion_id = retire_strategy(
        strategy_id=sid, reason="dashboard test", metrics_snapshot={"pf_20": 0.3}
    )

    flask_app = dash_app.create_app()
    client = flask_app.test_client()

    resp = client.get("/api/demotions")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "demotions" in payload
    ids = [d["id"] for d in payload["demotions"]]
    assert demotion_id in ids
    matching = [d for d in payload["demotions"] if d["id"] == demotion_id][0]
    assert matching["reactivated"] is False
    assert matching["symbol"] == "EE"

    resp1 = client.post(f"/api/demotions/{demotion_id}/reactivate")
    assert resp1.status_code == 200
    j1 = resp1.get_json()
    assert j1["ok"] is True
    assert isinstance(j1["new_strategy_id"], int)

    resp2 = client.post(f"/api/demotions/{demotion_id}/reactivate")
    assert resp2.status_code == 400
    j2 = resp2.get_json()
    assert j2["ok"] is False
    assert "already reactivated" in j2["error"]
