"""ROSTER FLOOR guard (2026-09-22): the roster drained 25→4 because pruning ran
with no refill. Below the floor, retire_strategy() must refuse soft reasons and
allow only code-verified hard losers or structural retires."""
from __future__ import annotations

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
    monkeypatch.setenv("JUDAS_ROSTER_FLOOR", "3")
    from src.db.models import init_db
    init_db(path)
    return str(path)


def _seed(db_path: str, symbol: str = "MGC", family: str = "judas_1h") -> int:
    from src.strategy_registry import activate_seed_strategy
    return activate_seed_strategy(
        symbol=symbol, strategy_family=family,
        params={"symbol": symbol, "strategy_family": family, "execution_engine": "judas_native",
                "min_displacement_strength": 1.5, "min_displacement_body_ratio": 0.5,
                "max_sweep_age_bars": 2, "target_r": 2.0},
        metrics={"seeded": True}, notes="test seed",
    )


def _add_trades(db_path: str, sid: int, pnls: list[float]) -> None:
    from src.db.models import get_conn
    with get_conn(db_path) as conn:
        for i, p in enumerate(pnls):
            conn.execute(
                "INSERT INTO trades (symbol, direction, qty, entry_fill, exit_fill, pnl_dollars, "
                "status, opened_at, closed_at, strategy_id, strategy_family, strategy_version) "
                "VALUES ('MGC','long',1,100,101,?, 'closed', ?, ?, ?, 'judas_1h', 1)",
                (p, f"2026-09-0{1 + i % 9}T10:00:00Z", f"2026-09-0{1 + i % 9}T11:00:00Z", sid),
            )
        conn.commit()


def test_soft_retire_refused_below_floor(db_path):
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]   # 3 active == floor
    with pytest.raises(ValueError, match="ROSTER FLOOR"):
        sr.retire_strategy(strategy_id=ids[0], reason="Stale-fire: 16 days, zero fires",
                           metrics_snapshot={})
    assert sr.roster_health(db_path)["n_active"] == 3


def test_soft_retire_allowed_above_floor(db_path):
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF", "MCL")]  # 4 > floor 3
    sr.retire_strategy(strategy_id=ids[0], reason="Stale-fire: 16 days, zero fires",
                       metrics_snapshot={})
    assert sr.roster_health(db_path)["n_active"] == 3


def test_structural_retire_allowed_below_floor(db_path):
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]
    sr.retire_strategy(strategy_id=ids[1], reason="Duplicate-fire pair retire per audit",
                       metrics_snapshot={})
    assert sr.roster_health(db_path)["n_active"] == 2


def test_hard_loser_verified_in_code_allowed_below_floor(db_path):
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]
    _add_trades(db_path, ids[0], [-50.0] * 7)          # 7 consecutive losers
    sr.retire_strategy(strategy_id=ids[0], reason="regime shift", metrics_snapshot={})
    assert sr.roster_health(db_path)["n_active"] == 2


def test_claimed_loser_without_fills_refused_below_floor(db_path):
    """The LLM's reason text is not evidence — live fills are."""
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]
    with pytest.raises(ValueError, match="ROSTER FLOOR"):
        sr.retire_strategy(strategy_id=ids[0], reason="pf_20=0.2 confirmed loser (trust me)",
                           metrics_snapshot={"pf_20": 0.2})


def test_force_bypasses_floor(db_path):
    from src import strategy_registry as sr
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]
    sr.retire_strategy(strategy_id=ids[0], reason="human override", metrics_snapshot={}, force=True)
    assert sr.roster_health(db_path)["n_active"] == 2


def test_floor_disabled_by_env(db_path, monkeypatch):
    from src import strategy_registry as sr
    monkeypatch.setenv("JUDAS_ROSTER_FLOOR", "0")
    ids = [_seed(db_path, s) for s in ("MGC", "MNQ", "ZF")]
    sr.retire_strategy(strategy_id=ids[0], reason="stale", metrics_snapshot={})
    assert sr.roster_health(db_path)["n_active"] == 2


def test_roster_health_shape_and_briefings(db_path):
    from src import strategy_registry as sr
    from src.research.researcher_agent import _build_kickoff
    _seed(db_path, "MGC")
    h = sr.roster_health(db_path)
    assert h["n_active"] == 1 and h["below_floor"] is True and h["inflow_stalled"] is True
    assert "ZF" in h["uncovered_symbols"]
    k = _build_kickoff(db_path)
    assert "ROSTER HEALTH" in k and "BELOW FLOOR" in k and "LIBRARY" in k
    assert "REFILL" in sr.roster_health_line(db_path)
