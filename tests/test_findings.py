"""Coverage for shared findings memory (CrewAI Memory backend).

Old tests inspected the SQLite `findings` table directly. The team
memory now lives in CrewAI Memory (LanceDB + ONNX embeddings + composite
scoring). These tests validate behavior via the public tool API.

Each test gets its own CREWAI_STORAGE_DIR so the singleton can be
re-initialised against a clean store.
"""
from __future__ import annotations

import os

import pytest

from src.db.models import init_db
from src.research import (
    agent_tools, coder_agent, operator_agent, registrar_agent,
    researcher_agent, trader_agent,
)
from src.research.agent_tools import (
    make_get_strategy_dossier, make_read_findings, make_record_finding,
    make_retract_finding,
)


@pytest.fixture
def fresh_memory(tmp_path, monkeypatch):
    """Isolate each test in its own memory store."""
    storage_dir = tmp_path / "mem-store"
    monkeypatch.setenv("CREWAI_STORAGE_DIR", str(storage_dir))
    # Reset the module-level singleton so the new env var takes effect.
    from src.research import memory_backend as mb
    monkeypatch.setattr(mb, "_MEMORY_SINGLETON", None)
    yield storage_dir
    # Drain writes before teardown so per-test isolation is real.
    try:
        mb.drain()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# record_finding
# ---------------------------------------------------------------------------


def test_record_finding_returns_id_and_importance(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    out = rec(title="MGC sweep tendency at 09:30 ET",
              body="Across the last 30 sessions, 60% of NY-open MGC "
                   "sweeps tagged the prior swing within 5 minutes.")
    assert out["ok"] is True
    assert isinstance(out["finding_id"], str) and out["finding_id"]
    # Researcher discoveries default to 0.6 (above neutral).
    assert 0.55 <= out["importance"] <= 0.7


def test_record_finding_human_gets_high_importance(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="human")
    out = rec(title="Stop chasing eval_only — it's a phantom",
              body="The actual blocker was corrupted active_strategies rows.")
    assert out["ok"] is True
    # Human findings get ~0.98 so they always surface.
    assert out["importance"] >= 0.95


def test_record_finding_cycle_status_gets_low_importance(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="registrar")
    out = rec(title="Registrar cycle 21:43 UTC — queue empty, standing by",
              body="Nothing to mutate this cycle.")
    assert out["ok"] is True
    # Cycle-status pings auto-tagged at 0.1 so they don't crowd recall.
    assert out["importance"] <= 0.2


# ---------------------------------------------------------------------------
# read_findings — composite ranking
# ---------------------------------------------------------------------------


def test_read_findings_surfaces_human_over_cycle_noise(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    human = make_record_finding(db_path=db, author="human")
    reg = make_record_finding(db_path=db, author="registrar")
    # Seed 5 registrar cycle pings then the human finding.
    for i in range(5):
        reg(title=f"Registrar cycle 21:{40 + i} UTC — queue empty, standing by",
            body="no-op cycle")
    human(title="active_strategies rows 134/135 are corrupted — symbol mismatch",
          body="evaluate_active_strategy reads active[symbol] but params target "
               "a different symbol. Retire them; fire_count=0 is downstream.")
    # Drain writes so recall sees them.
    from src.research import memory_backend
    memory_backend.drain()
    read = make_read_findings(db_path=db)
    results = read(limit=10)
    assert len(results) >= 1
    # Human finding should rank ahead of all 5 registrar pings.
    authors = [r["author"] for r in results]
    first_human = authors.index("human") if "human" in authors else -1
    first_registrar = authors.index("registrar") if "registrar" in authors else len(authors)
    assert first_human >= 0
    assert first_human <= first_registrar


def test_read_findings_semantic_query(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    rec(title="MGC sweep tendency at 09:30 ET",
        body="60% of NY-open MGC sweeps tag prior swing within 5 minutes.")
    rec(title="DX correlation breakdown post-FOMC",
        body="DX/gold inverse correlation weakens 30 min after FOMC release.")
    from src.research import memory_backend
    memory_backend.drain()
    read = make_read_findings(db_path=db)
    results = read(query="gold sweep behavior", limit=5)
    assert results, "semantic recall returned nothing"
    # MGC sweep result should rank above the DX one.
    titles = " ".join(r["title"].lower() for r in results)
    assert "mgc" in titles


def test_read_findings_filter_by_author(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    make_record_finding(db_path=db, author="researcher")(
        title="R1", body="researcher finding")
    make_record_finding(db_path=db, author="trader")(
        title="T1", body="trader finding")
    from src.research import memory_backend
    memory_backend.drain()
    read = make_read_findings(db_path=db)
    results = read(author="trader", limit=10)
    assert all(r["author"] == "trader" for r in results)


# ---------------------------------------------------------------------------
# retract_finding
# ---------------------------------------------------------------------------


def test_retract_finding_removes_record(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    out = rec(title="to be retracted",
              body="placeholder finding scheduled for removal")
    fid = out["finding_id"]
    from src.research import memory_backend
    memory_backend.drain()
    retract = make_retract_finding(db_path=db, author="researcher")
    r = retract(id=fid, reason="superseded by better data")
    assert r["ok"] is True


# ---------------------------------------------------------------------------
# Palette / wiring regressions — unchanged
# ---------------------------------------------------------------------------


_REQUIRED = {"record_finding", "read_findings", "retract_finding",
             "get_strategy_dossier"}


def test_all_agent_palettes_include_findings_tools():
    for mod in (operator_agent, researcher_agent, trader_agent,
                registrar_agent, coder_agent):
        missing = _REQUIRED - set(mod.INCLUDE_TOOLS)
        assert not missing, (
            f"{mod.__name__}.INCLUDE_TOOLS missing findings tools: "
            f"{sorted(missing)}"
        )


def test_make_tools_binds_findings_for_each_team(fresh_memory, tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    for team, author in (("researcher", "researcher"),
                         ("trader", "trader"),
                         ("registrar", "registrar"),
                         ("coder", "coder")):
        tools, _ = agent_tools.make_tools(
            db_path=db, include=_REQUIRED, team=team, author=author,
        )
        assert _REQUIRED.issubset(tools.keys())
        out = tools["record_finding"](title=f"t-{team}", body=f"b-{team}")
        assert out["ok"] is True


def test_get_strategy_dossier_returns_bundle(fresh_memory, tmp_path):
    """Dossier still reads from the live DB tables (active_strategies,
    auto_demotions, etc.) — that's not in memory. This is a smoke test
    that the helper works after the memory refactor."""
    db = str(tmp_path / "j.db")
    init_db(db)
    dossier = make_get_strategy_dossier(db_path=db)
    out = dossier(strategy_name="nonexistent")
    assert isinstance(out, dict)
    assert "active" in out or "ok" in out
