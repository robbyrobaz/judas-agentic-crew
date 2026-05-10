"""Phase 2 regression tests for src.research.live_review."""
from __future__ import annotations

import importlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research import live_review  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_strategy(
    db_path: Path,
    *,
    symbol: str = "MGC",
    family: str = "judas_1h",
    name: str = "judas_base_1h",
    state: str = "active",
) -> int:
    init_db(str(db_path))
    params = {"strategy_name": name, "symbol": symbol}
    with get_conn(str(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json, state, activated_at_utc, notes)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                family,
                json.dumps(params),
                "{}",
                state,
                _utc_iso(datetime.now(timezone.utc)),
                "seed",
            ),
        )
        return int(cur.lastrowid)


def _seed_trades(
    db_path: Path,
    *,
    strategy_id: int,
    pnls: list[float],
    last_opened_days_ago: int = 0,
) -> None:
    """Insert closed trades; the *first* element of pnls is the most recent."""
    base = datetime.now(timezone.utc) - timedelta(days=last_opened_days_ago)
    with get_conn(str(db_path)) as conn:
        for i, pnl in enumerate(pnls):
            opened = base - timedelta(hours=i)
            closed = opened + timedelta(minutes=30)
            conn.execute(
                """
                INSERT INTO trades
                    (signal_id, strategy_id, symbol, direction, qty, entry_fill,
                     pnl_dollars, status, opened_at, closed_at)
                VALUES (NULL, ?, 'MGC', 'long', 1, 100.0, ?, 'closed', ?, ?)
                """,
                (strategy_id, float(pnl), _utc_iso(opened), _utc_iso(closed)),
            )


# ---------------------------------------------------------------------------
# compute_live_metrics tests
# ---------------------------------------------------------------------------


def test_compute_metrics_basic(tmp_path):
    db = tmp_path / "judas_crew.db"
    sid = _seed_strategy(db)
    # 20 closed: alternating to keep consec losers low. PF = 1200/400 = 3.0
    pnls = []
    wins_left = 12
    losses_left = 8
    while wins_left or losses_left:
        if wins_left:
            pnls.append(100.0)
            wins_left -= 1
        if losses_left:
            pnls.append(-50.0)
            losses_left -= 1
    _seed_trades(db, strategy_id=sid, pnls=pnls)

    m = live_review.compute_live_metrics(db_path=str(db), strategy_id=sid)
    assert m.n_closed_trades == 20
    assert m.pf_20 == pytest.approx(3.0)
    # expectancy = mean(pnl) / avg_loss = (1200-400)/20 / 50 = 40/50 = 0.8R
    assert m.expectancy_20 == pytest.approx(0.8)
    assert m.total_realized_pnl == pytest.approx(800.0)
    assert m.max_consec_losers <= 1
    assert m.days_since_last_fire is not None and m.days_since_last_fire >= 0


def test_compute_metrics_insufficient_data(tmp_path):
    db = tmp_path / "judas_crew.db"
    sid = _seed_strategy(db)
    _seed_trades(db, strategy_id=sid, pnls=[100.0, -50.0, 25.0])

    m = live_review.compute_live_metrics(db_path=str(db), strategy_id=sid)
    assert m.n_closed_trades == 3
    assert math.isnan(m.pf_20)
    assert math.isnan(m.expectancy_20)


# ---------------------------------------------------------------------------
# decide_action deterministic tests
# ---------------------------------------------------------------------------


def _metrics(**overrides) -> live_review.StrategyMetrics:
    base = dict(
        strategy_id=1,
        strategy_name="t",
        n_closed_trades=20,
        pf_20=1.5,
        expectancy_20=0.5,
        max_consec_losers=2,
        days_since_last_fire=1,
        total_realized_pnl=500.0,
    )
    base.update(overrides)
    return live_review.StrategyMetrics(**base)


def test_decide_action_deterministic_retire(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    m = _metrics(pf_20=0.4, max_consec_losers=7)
    d = live_review.decide_action(m)
    assert d.action == "retire"
    assert d.fallback_used is True


def test_decide_action_deterministic_keep(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    m = _metrics(pf_20=1.8, max_consec_losers=2, days_since_last_fire=1)
    d = live_review.decide_action(m)
    assert d.action == "keep"


def test_decide_action_deterministic_retune(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    m = _metrics(pf_20=1.0, max_consec_losers=2, days_since_last_fire=20)
    d = live_review.decide_action(m)
    assert d.action == "retune"


def test_decide_action_falls_back_on_llm_failure(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-test")

    def boom(_prompt: str) -> str:
        raise RuntimeError("network exploded")

    monkeypatch.setattr(live_review, "_call_llm", boom)

    m = _metrics(pf_20=1.8)
    d = live_review.decide_action(m)
    assert d.action == "keep"
    assert d.fallback_used is True
    assert "llm fallback" in d.reason


# ---------------------------------------------------------------------------
# OperatorFlow integration tests
# ---------------------------------------------------------------------------


crewai = pytest.importorskip("crewai")
_crewai_version = tuple(int(p) for p in crewai.__version__.split(".")[:2])
_skip_flow = pytest.mark.skipif(
    _crewai_version < (1, 8),
    reason=f"OperatorFlow requires crewai >= 1.8, found {crewai.__version__}",
)


def _reload_operator_flow():
    for name in list(sys.modules):
        if name == "src.flows.operator_flow":
            del sys.modules[name]
    return importlib.import_module("src.flows.operator_flow")


@_skip_flow
def test_morning_review_routes_to_retire(tmp_path, monkeypatch):
    db = tmp_path / "judas_crew.db"
    sid = _seed_strategy(db, name="bad_strat")
    # PF 0.5: 5 wins $50 + 15 losses $50 = +250 / -750 = 0.333
    _seed_trades(db, strategy_id=sid, pnls=[50.0] * 5 + [-50.0] * 15)

    monkeypatch.setenv(
        "JUDAS_OPERATOR_STATE_DB", str(tmp_path / "flow_state.db")
    )
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    module = _reload_operator_flow()

    retire_calls: list[dict] = []

    from src import strategy_registry

    def fake_retire(*, strategy_id: int, reason: str, metrics_snapshot: dict) -> int:
        retire_calls.append(
            {
                "strategy_id": strategy_id,
                "reason": reason,
                "metrics_snapshot": metrics_snapshot,
            }
        )
        return 999

    monkeypatch.setattr(
        strategy_registry, "retire_strategy", fake_retire, raising=False
    )

    flow = module.OperatorFlow()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})

    assert flow.state.decision == "retire"
    assert flow.state.findings is not None
    assert "retire_list" in flow.state.findings
    assert len(flow.state.findings["retire_list"]) == 1
    assert len(retire_calls) == 1
    assert retire_calls[0]["strategy_id"] == sid


@_skip_flow
def test_morning_review_routes_to_noop_when_clean(tmp_path, monkeypatch):
    db = tmp_path / "judas_crew.db"
    sid = _seed_strategy(db, name="good_strat")
    # PF ~3.0 — healthy, alternating to keep consec losers low
    pnls = []
    w, l = 12, 8
    while w or l:
        if w:
            pnls.append(100.0); w -= 1
        if l:
            pnls.append(-50.0); l -= 1
    _seed_trades(db, strategy_id=sid, pnls=pnls)

    monkeypatch.setenv(
        "JUDAS_OPERATOR_STATE_DB", str(tmp_path / "flow_state.db")
    )
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    module = _reload_operator_flow()
    # Force noop routing so Phase 5 explore triggers don't flip this test.
    monkeypatch.setattr(module, "_decide_explore_or_noop", lambda *, db_path: ("noop", None))

    flow = module.OperatorFlow()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})

    assert flow.state.decision == "noop"
