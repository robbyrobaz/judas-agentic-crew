"""Phase 1 regression tests for OperatorFlow.

Verifies:
1. Flow imports, instantiates and completes a kickoff() in under 60 s.
2. State is persisted to the SQLite DB pointed at by JUDAS_OPERATOR_STATE_DB.
3. A second kickoff with the same flow id resumes state and increments
   ``cycle_count`` from 1 to 2 across two distinct OperatorFlow instances.

External services (LLM, IBKR) are not touched — every leaf step is a stub.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Skip cleanly on CrewAI < 1.8 (the version that ships @persist + @human_feedback).
crewai = pytest.importorskip("crewai")
_crewai_version = tuple(int(p) for p in crewai.__version__.split(".")[:2])
pytestmark = pytest.mark.skipif(
    _crewai_version < (1, 8),
    reason=f"OperatorFlow requires crewai >= 1.8, found {crewai.__version__}",
)


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    """Point the OperatorFlow at an isolated SQLite DB and reload the module.

    The persistence backend is bound at decorator-evaluation time, so we must
    re-import the module after setting the env var to pick up the override.
    """
    db_path = tmp_path / "flow_state.db"
    monkeypatch.setenv("JUDAS_OPERATOR_STATE_DB", str(db_path))
    monkeypatch.setenv("JUDAS_DB_PATH", str(tmp_path / "judas_crew.db"))

    # Drop any cached import so the module-level @persist re-evaluates with
    # the new env var.
    for name in list(sys.modules):
        if name == "src.flows.operator_flow" or name.startswith(
            "src.flows.operator_flow."
        ):
            del sys.modules[name]

    module = importlib.import_module("src.flows.operator_flow")
    yield module, db_path


def _state_rows(db_path: Path) -> list[dict]:
    """Return all persisted state rows as decoded dicts (table-name agnostic)."""
    conn = sqlite3.connect(str(db_path))
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        rows: list[dict] = []
        for table in tables:
            cur = conn.execute(f"SELECT * FROM {table}")
            cols = [c[0] for c in cur.description]
            for raw in cur.fetchall():
                record = dict(zip(cols, raw))
                # Decode any JSON-ish state payload.
                for key, value in list(record.items()):
                    if isinstance(value, str) and value.startswith("{"):
                        try:
                            record[key] = json.loads(value)
                        except json.JSONDecodeError:
                            pass
                rows.append(record)
        return rows
    finally:
        conn.close()


def _find_state_payload(rows: list[dict]) -> dict | None:
    """Return the most-recently-persisted state payload.

    CrewAI's SQLite backend writes one row per (flow_uuid, method) checkpoint.
    The numeric primary-key column orders rows by insertion, so the highest
    ``id`` is the latest checkpoint.
    """
    candidates: list[tuple[int, dict]] = []
    for row in rows:
        seq = row.get("id") if isinstance(row.get("id"), int) else 0
        for value in row.values():
            if isinstance(value, dict) and "cycle_count" in value:
                candidates.append((seq, value))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def test_operator_flow_kickoff_completes_and_persists(state_db, monkeypatch):
    module, db_path = state_db
    # Force noop routing so the Phase 5 explore triggers don't flip this
    # Phase 1 routing test (empty fixture DB looks 'stale' to the explorer).
    monkeypatch.setattr(module, "_decide_explore_or_noop", lambda *, db_path: ("noop", None))
    flow = module.OperatorFlow()

    start = time.monotonic()
    flow.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})
    elapsed = time.monotonic() - start

    assert elapsed < 60, f"kickoff exceeded 60s budget: {elapsed:.2f}s"
    assert flow.state.cycle_count == 1
    assert flow.state.last_run_utc is not None
    assert flow.state.findings is not None
    assert flow.state.decision == "noop"

    assert db_path.exists(), "persistence DB was not created"
    payload = _find_state_payload(_state_rows(db_path))
    assert payload is not None, "no persisted state row with cycle_count found"
    assert payload["cycle_count"] == 1


def test_operator_flow_resumes_state_across_instances(state_db):
    module, db_path = state_db

    flow_a = module.OperatorFlow()
    flow_a.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})
    assert flow_a.state.cycle_count == 1

    # Fresh instance — must hydrate state from the persisted row and increment.
    flow_b = module.OperatorFlow()
    flow_b.kickoff(inputs={"id": module.OPERATOR_FLOW_ID})
    assert flow_b.state.cycle_count == 2, (
        "second kickoff did not resume from persisted state"
    )

    payload = _find_state_payload(_state_rows(db_path))
    assert payload is not None
    assert payload["cycle_count"] == 2
