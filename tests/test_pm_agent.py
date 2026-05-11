"""Tests for src.research.pm_agent — the loose-mandate PM agent.

External services are never reached: every test monkeypatches the
``_call_llm`` seam (and where relevant the ``_place_bracket`` broker seam).
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research import pm_agent  # noqa: E402
from src import strategy_registry  # noqa: E402


# Conftest sets JUDAS_PM_AGENT_INHIBIT=1 + pops MINIMAX_API_KEY so the wider
# suite never reaches the LLM. These pm_agent tests exercise the agent loop
# directly (with `_call_llm` monkeypatched), so re-enable per-test.
@pytest.fixture(autouse=True)
def _enable_pm_agent_for_tests(monkeypatch):
    monkeypatch.delenv("JUDAS_PM_AGENT_INHIBIT", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_active(db_path: str, *, params: dict | None = None) -> int:
    init_db(db_path)
    p = params or {"strategy_name": "judas_base_1h", "symbol": "MGC"}
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES ('MGC','judas_1h',1,?, '{}', 'active', '2026-01-01T00:00:00Z', 'seed')
            """,
            (json.dumps(p),),
        )
        return int(cur.lastrowid)


def _seed_candidate(db_path: str) -> int:
    init_db(db_path)
    return strategy_registry.create_candidate(
        symbol="MGC",
        strategy_family="judas_1h",
        params={"strategy_name": "candidate_v2", "symbol": "MGC"},
        metrics={"pf": 1.5},
        decision="test",
        rationale="seed",
    )


def _fake_llm_response(*, tool_name: str | None = None, args: dict | None = None,
                       content: str = "done"):
    """Build a minimal litellm-style response object."""
    if tool_name is None:
        return {"choices": [{"message": {"role": "assistant", "content": content,
                                         "tool_calls": []}}]}
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(args or {}),
                            },
                        }
                    ],
                }
            }
        ]
    }


def _scripted_llm(steps: list):
    """Return a callable that yields one response per call from ``steps``."""
    seq = list(steps)
    calls = {"n": 0}

    def fake_call_llm(**_kwargs):
        calls["n"] += 1
        if not seq:
            # default: terminate
            return _fake_llm_response(content="done")
        return seq.pop(0)

    fake_call_llm.calls = calls
    return fake_call_llm


# ---------------------------------------------------------------------------
# 1) System prompt is the loose mandate (regression against re-narrowing)
# ---------------------------------------------------------------------------


def test_loose_mandate_in_system_prompt():
    sp = pm_agent.PM_SYSTEM_PROMPT.lower()
    assert "elite futures trader" in sp
    assert "make as much money" in sp
    # Regression: forbidden phrases that would re-narrow the mandate.
    forbidden = [
        "validate before",
        "every drop must",
        "rolling waiting room",
    ]
    for phrase in forbidden:
        assert phrase not in sp, f"prompt re-narrowed with: {phrase!r}"


# ---------------------------------------------------------------------------
# 2 + 3) query_db rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO trades VALUES (1)",
        "UPDATE trades SET status='closed'",
        "DELETE FROM trades",
        "PRAGMA table_info(trades)",
        "ATTACH DATABASE 'foo' AS bar",
        "  /* hi */ INSERT INTO x VALUES (1)",
        "-- comment\nDROP TABLE foo",
    ],
)
def test_query_db_rejects_non_select(tmp_path, sql):
    db = tmp_path / "j.db"
    init_db(str(db))
    tools = pm_agent._make_tools(db_path=str(db))
    out = tools["query_db"](sql=sql)
    assert out["ok"] is False
    assert "rows" in out and out["rows"] == []


def test_query_db_rejects_multi_statement(tmp_path):
    db = tmp_path / "j.db"
    init_db(str(db))
    tools = pm_agent._make_tools(db_path=str(db))
    out = tools["query_db"](sql="SELECT 1; DROP TABLE x")
    assert out["ok"] is False
    # Trailing-only semicolon should be permitted though.
    out2 = tools["query_db"](sql="SELECT 1;")
    assert out2["ok"] is True


# ---------------------------------------------------------------------------
# 4) retire via tool writes auto_demotions row
# ---------------------------------------------------------------------------


def test_retire_via_tool_writes_demotion_row(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")
    sid = _seed_active(str(db))

    fake = _scripted_llm([
        _fake_llm_response(tool_name="retire_strategy",
                           args={"id": sid, "reason": "underperforming"}),
        _fake_llm_response(content="retired."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm", fake)

    result = pm_agent.run_pm_decision(db_path=str(db), turn_budget=5, time_budget_s=30)
    assert result.success
    assert any(a.action == "retire_strategy" for a in result.actions_taken)

    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT strategy_id, reason FROM auto_demotions WHERE strategy_id = ?",
            (sid,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == sid


# ---------------------------------------------------------------------------
# 5) promote via tool atomic
# ---------------------------------------------------------------------------


def test_promote_via_tool_atomic(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")
    sid = _seed_active(str(db))
    cid = _seed_candidate(str(db))

    fake = _scripted_llm([
        _fake_llm_response(tool_name="promote_candidate",
                           args={"id": cid, "notes": "go"}),
        _fake_llm_response(content="promoted."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm", fake)

    result = pm_agent.run_pm_decision(db_path=str(db), turn_budget=5, time_budget_s=30)
    assert result.success

    with sqlite3.connect(str(db)) as conn:
        active = conn.execute(
            "SELECT id, version FROM active_strategies WHERE state='active'"
        ).fetchall()
        retired = conn.execute(
            "SELECT id FROM active_strategies WHERE id = ? AND state='retired'",
            (sid,),
        ).fetchall()
    assert len(active) == 1
    assert active[0][1] == 2  # version bump
    assert len(retired) == 1


# ---------------------------------------------------------------------------
# 6) modify_strategy_params is atomic retire+promote with v+1
# ---------------------------------------------------------------------------


def test_modify_strategy_params_is_atomic_retire_promote(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")
    sid = _seed_active(str(db))

    new_params = {"strategy_name": "judas_base_1h", "symbol": "MGC", "target_r": 3.0}
    fake = _scripted_llm([
        _fake_llm_response(
            tool_name="modify_strategy_params",
            args={"id": sid, "new_params": new_params, "rationale": "tighten"},
        ),
        _fake_llm_response(content="modified."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm", fake)

    result = pm_agent.run_pm_decision(db_path=str(db), turn_budget=5, time_budget_s=30)
    assert result.success

    with sqlite3.connect(str(db)) as conn:
        # auto_demotions row exists for old id
        dem = conn.execute(
            "SELECT version, reason FROM auto_demotions WHERE strategy_id = ?",
            (sid,),
        ).fetchall()
        assert len(dem) == 1
        assert dem[0][0] == 1
        # new active row at version=2
        active = conn.execute(
            "SELECT id, version, params_json FROM active_strategies WHERE state='active'"
        ).fetchall()
        assert len(active) == 1
        assert active[0][1] == 2
        params = json.loads(active[0][2])
        assert params["target_r"] == 3.0
        # old row retired
        old_state = conn.execute(
            "SELECT state FROM active_strategies WHERE id = ?", (sid,)
        ).fetchone()
        assert old_state[0] == "retired"


# ---------------------------------------------------------------------------
# 7 + 8) place_paper_order routes through broker seam; honors dry_run
# ---------------------------------------------------------------------------


def test_place_paper_order_routes_through_existing_broker(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    init_db(str(db))
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")

    captured = {}

    def fake_broker(**kwargs):
        captured.update(kwargs)
        return {
            "parent_order_id": 111, "tp_order_id": 222, "sl_order_id": 333,
            "local_symbol": "MGCJ26", "status": "Submitted",
        }

    monkeypatch.setattr(pm_agent, "_place_bracket", fake_broker)
    monkeypatch.setattr(pm_agent, "_BROKER_DRY_RUN", False)

    args = {
        "symbol": "MGC", "side": "BUY", "quantity": 1,
        "stop_price": 1990.0, "target_price": 2010.0,
        "rationale": "high conviction sweep+CHoCH",
    }
    fake = _scripted_llm([
        _fake_llm_response(tool_name="place_paper_order", args=args),
        _fake_llm_response(content="placed."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm", fake)

    result = pm_agent.run_pm_decision(db_path=str(db), turn_budget=5, time_budget_s=30)
    assert result.success
    # Broker seam was hit with the correct trade kwargs.
    assert captured["symbol"] == "MGC"
    assert captured["side"] == "BUY"
    assert captured["quantity"] == 1
    assert captured["stop_price"] == 1990.0
    assert captured["target_price"] == 2010.0
    # signals row inserted
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy_family = 'pm_agent'"
        ).fetchone()[0]
    assert n == 1
    # action recorded with broker ids
    placed = [a for a in result.actions_taken if a.action == "place_paper_order"]
    assert placed and placed[0].tool_result.get("ok") is True
    assert 111 in placed[0].tool_result["ibkr_order_ids"]


def test_place_paper_order_skips_in_dry_run(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    init_db(str(db))
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")

    def boom(**_):
        raise AssertionError("broker should not be called in dry_run")

    monkeypatch.setattr(pm_agent, "_place_bracket", boom)
    monkeypatch.setattr(pm_agent, "_BROKER_DRY_RUN", True)

    args = {
        "symbol": "MGC", "side": "BUY", "quantity": 1,
        "stop_price": 1990.0, "target_price": 2010.0,
        "rationale": "test",
    }
    fake = _scripted_llm([
        _fake_llm_response(tool_name="place_paper_order", args=args),
        _fake_llm_response(content="ok."),
    ])
    monkeypatch.setattr(pm_agent, "_call_llm", fake)

    result = pm_agent.run_pm_decision(db_path=str(db), turn_budget=5, time_budget_s=30)
    placed = [a for a in result.actions_taken if a.action == "place_paper_order"]
    assert placed
    tr = placed[0].tool_result
    assert tr.get("ok") is False
    assert tr.get("reason") == "dry_run"


# ---------------------------------------------------------------------------
# 9) regression: no run_python / run_shell tools exist
# ---------------------------------------------------------------------------


def test_no_run_python_tool_exists(tmp_path):
    db = tmp_path / "j.db"
    init_db(str(db))
    tools = pm_agent._make_tools(db_path=str(db))
    schemas = pm_agent._tool_schemas()
    schema_names = {s["function"]["name"] for s in schemas}
    forbidden = {"run_python", "run_shell", "exec", "eval"}
    assert forbidden.isdisjoint(set(tools.keys())), tools.keys()
    assert forbidden.isdisjoint(schema_names), schema_names


# ---------------------------------------------------------------------------
# 10 + 11) budgets enforced
# ---------------------------------------------------------------------------


def test_turn_budget_enforced(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    init_db(str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")

    # Always return a tool call → never terminates on its own.
    def loop_llm(**_kwargs):
        return _fake_llm_response(
            tool_name="get_active_strategies", args={}
        )

    monkeypatch.setattr(pm_agent, "_call_llm", loop_llm)
    result = pm_agent.run_pm_decision(
        db_path=str(db), turn_budget=2, time_budget_s=30
    )
    assert result.turns_used == 2


def test_time_budget_enforced(tmp_path, monkeypatch):
    """When a positive time_budget_s is set, the loop stops once exceeded.
    time_budget_s=0 disables the cap (fully autonomous default)."""
    import time as _time
    db = tmp_path / "j.db"
    init_db(str(db))
    monkeypatch.setenv("MINIMAX_API_KEY", "fake")

    def slow_llm(**_kwargs):
        _time.sleep(0.05)
        return _fake_llm_response(tool_name="get_active_strategies", args={})

    monkeypatch.setattr(pm_agent, "_call_llm", slow_llm)
    result = pm_agent.run_pm_decision(
        db_path=str(db), turn_budget=100, time_budget_s=1
    )
    # Slow LLM (~50ms each) under 1s budget → caps out well below 100 turns.
    assert result.turns_used < 100
    assert result.elapsed_s < 5.0


# ---------------------------------------------------------------------------
# 12) no API key → fallback does NOTHING aggressive
# ---------------------------------------------------------------------------


def test_no_api_key_fallback_does_nothing(tmp_path, monkeypatch):
    db = tmp_path / "j.db"
    init_db(str(db))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    def boom(**_):
        raise AssertionError("LLM must not be called without API key")

    monkeypatch.setattr(pm_agent, "_call_llm", boom)

    result = pm_agent.run_pm_decision(db_path=str(db))
    assert result.fallback_used is True
    assert result.actions_taken == []
    assert "no actions" in result.narrative.lower()


# ---------------------------------------------------------------------------
# 13) morning_review uses pm_agent
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
def test_morning_review_uses_pm_agent(tmp_path, monkeypatch):
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    monkeypatch.setenv("JUDAS_OPERATOR_STATE_DB", str(tmp_path / "flow_state.db"))
    monkeypatch.setenv("JUDAS_DB_PATH", str(db))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    module = _reload_operator_flow()

    calls: list[dict] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return pm_agent.PMDecisionResult(
            success=True,
            actions_taken=[
                pm_agent.PMAction(
                    action="retire_strategy",
                    target_id=42,
                    payload={"id": 42, "reason": "test"},
                    rationale="test",
                    tool_result={"ok": True, "demotion_id": 7},
                )
            ],
            narrative="PM cycle ran.",
            turns_used=3,
            elapsed_s=1.2,
            fallback_used=False,
            raw_messages=[],
            error=None,
        )

    # Phase 10: morning_review delegates via operator_agent.
    from src.research import operator_agent
    def fake_op_run(**kwargs):
        calls.append(kwargs)
        return operator_agent.OperatorDecisionResult(
            success=True,
            delegations=[],
            actions_taken=[
                pm_agent.PMAction(
                    action="retire_strategy",
                    target_id=42,
                    payload={"id": 42, "reason": "test"},
                    rationale="test",
                    tool_result={"ok": True, "demotion_id": 7},
                )
            ],
            narrative="PM cycle ran.",
            turns_used=3,
            elapsed_s=1.2,
            fallback_used=False,
            raw_messages=[],
            error=None,
        )
    monkeypatch.setattr(operator_agent, "run_operator_decision", fake_op_run,
                        raising=True)

    flow = module.OperatorFlow()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})

    assert len(calls) == 1
    assert flow.state.findings is not None
    pm_payload = flow.state.findings.get("pm_result")
    assert pm_payload is not None
    assert pm_payload["narrative"] == "PM cycle ran."
    assert len(pm_payload["actions_taken"]) == 1


# ---------------------------------------------------------------------------
# 14) brief includes PM narrative + bullets
# ---------------------------------------------------------------------------


def test_brief_includes_pm_narrative(tmp_path):
    db = tmp_path / "j.db"
    init_db(str(db))

    pm_payload = {
        "success": True,
        "narrative": "Kept MGC on track; trimmed two stale bets.",
        "actions_taken": [
            {
                "action": "retire_strategy",
                "target_id": 7,
                "payload": {"id": 7, "reason": "stale"},
                "rationale": "no fires in 14d",
                "tool_result": {"ok": True, "demotion_id": 1},
            },
            {
                "action": "place_paper_order",
                "target_id": None,
                "payload": {"symbol": "MGC", "side": "BUY", "quantity": 1,
                            "stop_price": 1990.0, "target_price": 2010.0,
                            "rationale": "sweep+CHoCH"},
                "rationale": "sweep+CHoCH",
                "tool_result": {"ok": False, "reason": "dry_run"},
            },
        ],
        "turns_used": 5,
        "elapsed_s": 4.4,
        "fallback_used": False,
        "error": None,
    }

    from src.research import brief as brief_mod

    composed = brief_mod.compose_daily_brief(
        db_path=str(db), brief_date="2026-05-09", pm_result=pm_payload,
    )
    md = composed["content_md"]
    assert "## Operator Decisions" in md
    assert "Kept MGC on track" in md
    assert "retire_strategy" in md
    assert "place_paper_order" in md
    assert "no fires in 14d" in md
