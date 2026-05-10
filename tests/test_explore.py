"""Phase 5 tests — explore planner + executor + flow integration."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research import explore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_active(
    db_path: Path,
    *,
    name: str,
    symbol: str = "MGC",
    family: str = "judas_1h",
    activated_at: datetime | None = None,
    max_sweep_age_bars: int = 2,
) -> int:
    init_db(str(db_path))
    activated_at = activated_at or datetime.now(timezone.utc)
    params = {
        "strategy_name": name,
        "symbol": symbol,
        "max_sweep_age_bars": max_sweep_age_bars,
    }
    with get_conn(str(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES (?, ?, 1, ?, '{}', 'active', ?, 'seed')
            """,
            (symbol, family, json.dumps(params), _utc_iso(activated_at)),
        )
        return int(cur.lastrowid)


def _seed_demotion(
    db_path: Path,
    *,
    strategy_id: int,
    ts: datetime,
    symbol: str = "MGC",
    reason: str = "pf_20<0.9",
) -> None:
    init_db(str(db_path))
    with get_conn(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO auto_demotions
                (ts_utc, strategy_id, symbol, strategy_family, version,
                 params_json, metrics_snapshot_json, reason)
            VALUES (?, ?, ?, 'judas_1h', 1, '{}', '{}', ?)
            """,
            (_utc_iso(ts), strategy_id, symbol, reason),
        )


def _seed_brief(
    db_path: Path,
    *,
    brief_date: str,
    vol_regime: str = "mid",
    trend: str = "mixed",
    n_surprises: int = 0,
    surprise_strategy_id: int | None = None,
) -> None:
    init_db(str(db_path))
    surprises: list[dict] = []
    for _ in range(n_surprises):
        surprises.append(
            {
                "strategy_id": surprise_strategy_id or 1,
                "name": "x",
                "expected_pnl": 0.0,
                "actual_pnl": 100.0,
                "z": 3.0,
            }
        )
    summary = {
        "fires": 1,
        "fills": 1,
        "pnl_total": 0.0,
        "pnl_by_strategy": [],
        "regime": {"vol_regime": vol_regime, "trend": trend, "leaders": []},
        "surprises": surprises,
        "recommended_actions": [],
    }
    with get_conn(str(db_path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO daily_briefs
                (brief_date, created_at_utc, content_md, summary_json)
            VALUES (?, ?, ?, ?)
            """,
            (brief_date, _utc_iso(datetime.now(timezone.utc)), "# brief", json.dumps(summary)),
        )


# ---------------------------------------------------------------------------
# 1. context
# ---------------------------------------------------------------------------


def test_gather_context_basic(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    now = datetime.now(timezone.utc)
    s1 = _seed_active(db, name="s1", symbol="MGC", activated_at=now - timedelta(days=2))
    _seed_active(db, name="s2", symbol="MNQ", activated_at=now - timedelta(days=4))
    _seed_active(db, name="s3", symbol="MCL", activated_at=now - timedelta(days=10))
    _seed_demotion(db, strategy_id=s1, ts=now - timedelta(days=3))
    _seed_brief(db, brief_date=(now - timedelta(days=1)).date().isoformat())
    _seed_brief(db, brief_date=(now - timedelta(days=2)).date().isoformat())

    ctx = explore.gather_explore_context(db_path=str(db))
    assert ctx["n_active"] == 3
    assert {a["symbol"] for a in ctx["active_strategies"]} == {"MGC", "MNQ", "MCL"}
    assert len(ctx["briefs_7d"]) == 2
    assert len(ctx["demotions_30d"]) >= 1
    serialized = json.dumps(ctx, default=str)
    assert len(serialized.encode("utf-8")) < 4096, f"context too large: {len(serialized)}"


# ---------------------------------------------------------------------------
# 2,3. deterministic planner branches
# ---------------------------------------------------------------------------


def test_plan_experiment_deterministic_no_turnover(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    ctx = {
        "active_strategies": [
            {"id": 1, "symbol": "MGC", "strategy_family": "judas_1h",
             "strategy_name": "s1", "max_sweep_age_bars": 2,
             "activated_at_utc": "2026-04-01T00:00:00Z"}
        ],
        "no_turnover_7d": True,
        "briefs_7d": [],
        "leaderboard_top5": [
            {"id": "met_rsi_20_80_15_10", "symbol": "MET", "kind": "zoo",
             "type": "rsi", "pf": 4.7, "trades": 14, "params": {}}
        ],
    }
    plan = explore.plan_experiment(context=ctx)
    assert plan.tool == "pair_sweep"
    assert plan.symbol == "MET"
    assert plan.fallback_used is True


def test_plan_experiment_deterministic_high_vol(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    ctx = {
        "active_strategies": [
            {"id": 1, "symbol": "MGC", "strategy_family": "judas_1h",
             "strategy_name": "s1", "max_sweep_age_bars": 2,
             "activated_at_utc": "2026-05-01T00:00:00Z"},
            {"id": 2, "symbol": "MNQ", "strategy_family": "judas_1h",
             "strategy_name": "s2", "max_sweep_age_bars": 5,  # loosest
             "activated_at_utc": "2026-05-01T00:00:00Z"},
        ],
        "no_turnover_7d": False,
        "briefs_7d": [
            {"brief_date": "2026-05-08", "regime": {"vol_regime": "high", "trend": "trending"},
             "n_surprises": 0, "surprise_strategy_ids": []}
        ],
        "leaderboard_top5": [],
    }
    plan = explore.plan_experiment(context=ctx)
    assert plan.tool == "judas_threshold_sweep"
    assert plan.symbol == "MNQ"
    assert plan.fallback_used is True


# ---------------------------------------------------------------------------
# 4,5. LLM fallback paths
# ---------------------------------------------------------------------------


def test_plan_experiment_falls_back_on_llm_failure(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")

    def raiser(prompt):
        raise RuntimeError("network down")

    monkeypatch.setattr(explore, "_call_llm", raiser)
    ctx = {
        "active_strategies": [],
        "no_turnover_7d": False,
        "briefs_7d": [],
        "leaderboard_top5": [],
    }
    plan = explore.plan_experiment(context=ctx)
    assert plan.fallback_used is True
    assert plan.tool in explore.ALLOWED_TOOLS


def test_plan_experiment_validates_tool_allowlist(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    monkeypatch.setattr(
        explore,
        "_call_llm",
        lambda prompt: json.dumps(
            {"tool": "rm_rf", "symbol": "MGC", "params": {}, "rationale": "evil"}
        ),
    )
    ctx = {
        "active_strategies": [],
        "no_turnover_7d": True,
        "briefs_7d": [],
        "leaderboard_top5": [
            {"id": "x", "symbol": "MET", "kind": "zoo", "type": "rsi",
             "pf": 4.7, "trades": 10, "params": {}}
        ],
    }
    plan = explore.plan_experiment(context=ctx)
    assert plan.tool != "rm_rf"
    assert plan.fallback_used is True
    assert plan.tool in explore.ALLOWED_TOOLS


# ---------------------------------------------------------------------------
# 6,7. executor dispatch + timeout
# ---------------------------------------------------------------------------


def test_execute_plan_dispatch(monkeypatch, tmp_path):
    captured: dict = {}

    class FakeTool:
        def run(self, input_json: str) -> str:
            captured["input_json"] = input_json
            return json.dumps({"experiment_id": 99, "ok": True})

    from src.tools import research_tools as rt

    monkeypatch.setattr(rt, "judas_parameter_sweep_tool", FakeTool())

    plan = explore.ExplorePlan(
        tool="judas_parameter_sweep",
        symbol="MGC",
        params={"max_combinations": 16},
        rationale="test",
        fallback_used=True,
    )
    result = explore.execute_plan(plan=plan, db_path=str(tmp_path / "j.db"))
    assert result["ok"] is True
    assert result["experiment_id"] == 99
    payload = json.loads(captured["input_json"])
    assert payload["symbol"] == "MGC"
    assert payload["max_combinations"] == 16


def test_execute_plan_timeout(monkeypatch, tmp_path):
    class SlowTool:
        def run(self, input_json: str) -> str:
            time.sleep(11)
            return "{}"

    from src.tools import research_tools as rt

    monkeypatch.setattr(rt, "judas_parameter_sweep_tool", SlowTool())

    plan = explore.ExplorePlan(
        tool="judas_parameter_sweep",
        symbol="MGC",
        params={},
        rationale="test",
        fallback_used=True,
    )
    result = explore.execute_plan(
        plan=plan, db_path=str(tmp_path / "j.db"), timeout_s=0.1
    )
    assert result["ok"] is False
    assert "timeout" in (result.get("error") or "").lower()


# ---------------------------------------------------------------------------
# 8,9,10. flow integration
# ---------------------------------------------------------------------------


def _import_flow_with_db(tmp_path, monkeypatch):
    import importlib

    state_db = tmp_path / "flow_state.db"
    judas_db = tmp_path / "judas.db"
    monkeypatch.setenv("JUDAS_OPERATOR_STATE_DB", str(state_db))
    monkeypatch.setenv("JUDAS_DB_PATH", str(judas_db))
    for name in list(sys.modules):
        if name == "src.flows.operator_flow" or name.startswith("src.flows.operator_flow."):
            del sys.modules[name]
    return importlib.import_module("src.flows.operator_flow"), judas_db


def test_morning_review_routes_to_explore_on_weekend(tmp_path, monkeypatch):
    crewai = pytest.importorskip("crewai")
    if tuple(int(p) for p in crewai.__version__.split(".")[:2]) < (1, 8):
        pytest.skip("requires crewai>=1.8")

    module, judas_db = _import_flow_with_db(tmp_path, monkeypatch)
    init_db(str(judas_db))

    # Force "Sunday in ET".
    sunday = datetime(2026, 5, 10, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(module, "_now_et", lambda: sunday)

    decision, reason = module._decide_explore_or_noop(db_path=str(judas_db))
    assert decision == "explore"
    assert reason == "weekend_research"


def test_morning_review_routes_to_explore_on_stale_active_set(tmp_path, monkeypatch):
    crewai = pytest.importorskip("crewai")
    if tuple(int(p) for p in crewai.__version__.split(".")[:2]) < (1, 8):
        pytest.skip("requires crewai>=1.8")

    module, judas_db = _import_flow_with_db(tmp_path, monkeypatch)
    # Force a weekday (Wed) so the weekend trigger doesn't dominate.
    weekday = datetime(2026, 5, 6, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(module, "_now_et", lambda: weekday)

    now = datetime.now(timezone.utc)
    _seed_active(judas_db, name="s_old", symbol="MGC",
                 activated_at=now - timedelta(days=10))

    decision, reason = module._decide_explore_or_noop(db_path=str(judas_db))
    assert decision == "explore"
    assert reason == "stale_active_set"


@pytest.mark.skip(
    reason="PM-agent era: morning_review no longer routes to 'explore'. "
    "explore_step remains a legacy listener but the router never triggers "
    "it — the PM agent decides whether to run experiments via tool calls "
    "inside run_pm_decision instead."
)
def test_explore_step_falls_through_on_error(tmp_path, monkeypatch):
    crewai = pytest.importorskip("crewai")
    if tuple(int(p) for p in crewai.__version__.split(".")[:2]) < (1, 8):
        pytest.skip("requires crewai>=1.8")

    module, judas_db = _import_flow_with_db(tmp_path, monkeypatch)
    init_db(str(judas_db))

    # Make execute_plan blow up; ensure flow still completes and brief tries to write.
    from src.research import explore as _explore

    def boom(**kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(_explore, "execute_plan", boom)

    # Force routing to explore via weekend.
    sunday = datetime(2026, 5, 10, 12, 0, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(module, "_now_et", lambda: sunday)

    flow = module.OperatorFlow()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})

    # Decision should be 'explore'; findings.experiment recorded the failure;
    # write_brief_step should still have run (no exception escaped).
    assert flow.state.decision == "explore"
    findings = flow.state.findings or {}
    assert "experiment" in findings
    outcome = findings["experiment"]["outcome"]
    assert outcome["ok"] is False
    assert "kaboom" in (outcome.get("error") or "")
