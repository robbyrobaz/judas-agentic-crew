"""Atomicity + validation tests for promote_candidate.

Phase 2 deliverable: BEGIN IMMEDIATE wrap + params_json schema validation
prevent partial state and reject malformed candidates.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
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


def _seed_active(db_path: str, *, symbol: str = "MGC", family: str = "judas_1h") -> int:
    from src.strategy_registry import activate_seed_strategy

    return activate_seed_strategy(
        symbol=symbol,
        strategy_family=family,
        params={"symbol": symbol, "strategy_family": family},
        metrics={"seeded": True},
        notes="test seed",
    )


def _make_candidate(db_path: str, *, params_json: str, symbol: str = "MGC", family: str = "judas_1h") -> int:
    """Insert raw candidate row with arbitrary params_json (bypassing create_candidate's json.dumps)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.execute(
            """
            INSERT INTO strategy_candidates
                (ts_utc, symbol, strategy_family, source_experiment_id, params_json, metrics_json,
                 decision, rationale, status)
            VALUES (?, ?, ?, NULL, ?, ?, 'promote', 'test', 'candidate')
            """,
            ("2026-05-09T00:00:00Z", symbol, family, params_json, "{}"),
        )
        conn.commit()
        return int(cur.lastrowid)


def test_promote_rejects_bad_params_json_not_a_dict(db_path):
    from src.strategy_registry import promote_candidate, list_active_strategies

    _seed_active(db_path, symbol="ZZ", family="fam_a")
    cand = _make_candidate(db_path, params_json=json.dumps([1, 2, 3]), symbol="ZZ", family="fam_a")

    pre = list_active_strategies()
    pre_count = sum(1 for r in pre if r["symbol"] == "ZZ" and r["strategy_family"] == "fam_a")

    with pytest.raises(ValueError):
        promote_candidate(cand)

    post = list_active_strategies()
    post_count = sum(1 for r in post if r["symbol"] == "ZZ" and r["strategy_family"] == "fam_a")
    # Old active row was NOT retired, no new row was inserted.
    assert pre_count == post_count == 1


def test_promote_rejects_params_missing_required_keys(db_path):
    from src.strategy_registry import promote_candidate

    _seed_active(db_path, symbol="YY", family="fam_b")
    # Dict without 'symbol' or 'strategy_name'/'strategy_family'.
    cand = _make_candidate(
        db_path, params_json=json.dumps({"foo": "bar"}), symbol="YY", family="fam_b"
    )
    with pytest.raises(ValueError):
        promote_candidate(cand)


def test_promote_concurrent_no_zero_active_window(db_path):
    """Spawn two threads — one polls list_active_strategies, the other promotes.
    Reader must never observe zero active rows for (symbol, family).
    """
    from src.strategy_registry import (
        create_candidate,
        list_active_strategies,
        promote_candidate,
    )

    symbol, family = "XX", "fam_c"
    _seed_active(db_path, symbol=symbol, family=family)
    cand_id = create_candidate(
        symbol=symbol,
        strategy_family=family,
        params={"symbol": symbol, "strategy_family": family, "strategy_name": "v2"},
        metrics={"profit_factor": 1.5},
        decision="promote",
        rationale="bench",
    )

    barrier = threading.Barrier(2)
    observations: list[int] = []
    errors: list[BaseException] = []

    def reader():
        try:
            barrier.wait()
            for _ in range(100):
                rows = list_active_strategies()
                count = sum(
                    1 for r in rows if r["symbol"] == symbol and r["strategy_family"] == family
                )
                observations.append(count)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        try:
            barrier.wait()
            promote_candidate(cand_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_reader = threading.Thread(target=reader)
    t_writer = threading.Thread(target=writer)
    t_reader.start()
    t_writer.start()
    t_reader.join(timeout=15)
    t_writer.join(timeout=15)

    assert not errors, errors
    assert observations, "reader observed nothing"
    assert min(observations) >= 1, f"observed zero-active window: {observations}"


def test_insert_active_strategy_creates_new_row(db_path):
    from src import strategy_registry as sr

    a = sr.insert_active_strategy(symbol="MGC", strategy_family="judas_1h")
    assert a.state == "active"
    assert a.symbol == "MGC"
    assert a.strategy_family == "judas_1h"
    assert a.version == 1


def test_insert_active_strategy_supersedes_prior_version(db_path):
    """A new version of the SAME (symbol, family) supersedes the prior active
    one — only the newest is active, so two versions of the same setup can't
    both fire the same bar. Cross-family diversity is unaffected."""
    from src import strategy_registry as sr

    a = sr.insert_active_strategy(symbol="MGC", strategy_family="judas_1h")
    b = sr.insert_active_strategy(symbol="MGC", strategy_family="judas_1h")
    c = sr.insert_active_strategy(symbol="MGC", strategy_family="judas_1h")

    actives = [r for r in sr.list_active_strategies()
               if r["symbol"] == "MGC" and r["strategy_family"] == "judas_1h"]
    assert len(actives) == 1
    assert actives[0]["version"] == c.version
    assert b.version == a.version + 1
    assert c.version == b.version + 1

    # A DIFFERENT family on the same symbol stays active alongside it.
    sr.insert_active_strategy(symbol="MGC", strategy_family="buffet_zoo",
                              params={"symbol": "MGC", "strategy_family": "buffet_zoo",
                                      "execution_engine": "buffet_zoo", "strategy_type": "rsi"})
    fams = {r["strategy_family"] for r in sr.list_active_strategies() if r["symbol"] == "MGC"}
    assert fams == {"judas_1h", "buffet_zoo"}


def test_insert_active_strategy_respects_passed_params(db_path):
    from src import strategy_registry as sr

    params = {"disp": 1.5, "target_r": 2.0, "min_sweep_ticks": 4}
    a = sr.insert_active_strategy(
        symbol="MNQ", strategy_family="judas_1h", params=params,
        notes="custom params seed",
    )
    assert a.params["disp"] == 1.5
    assert a.params["target_r"] == 2.0
    assert a.params["min_sweep_ticks"] == 4
    # symbol + family auto-injected into params.
    assert a.params["symbol"] == "MNQ"
    assert a.params["strategy_family"] == "judas_1h"
    assert a.notes == "custom params seed"


def test_custom_engine_requires_loadable_code_link(db_path):
    """2026-07-03 guard: engine='custom' rows must carry a custom_strategy_id
    that loads real code — promoting without one births a strategy that can
    never fire (the June idle-strategies bug)."""
    import pytest
    from src import strategy_registry as sr

    with pytest.raises(ValueError, match="custom_strategy_id"):
        sr.insert_active_strategy(
            symbol="MGC", strategy_family="custom_x",
            params={"execution_engine": "custom", "strategy_name": "x", "symbol": "MGC"},
        )
    with pytest.raises(ValueError, match="does not match an active row"):
        sr.insert_active_strategy(
            symbol="MGC", strategy_family="custom_x",
            params={"execution_engine": "custom", "custom_strategy_id": 999999,
                    "strategy_name": "x", "symbol": "MGC"},
        )
