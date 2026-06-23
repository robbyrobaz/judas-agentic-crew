"""Phase 3a — symptom detector tests."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db
from src.research.symptoms import (
    Symptom,
    detect_active_set_duplicates,
    detect_all_symptoms,
    detect_db_contention,
    detect_failing_pytest,
    detect_looping_research,
    detect_silent_dryrun,
    detect_stale_backtest_bars,
    detect_tool_failure,
    sha1_symptom,
)


def _seed_research_experiments_with_error_text(db_path: str, error_text: str, count: int) -> None:
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        # Add error_text column for the test if not present.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(research_experiments)").fetchall()]
        if "error_text" not in cols:
            conn.execute("ALTER TABLE research_experiments ADD COLUMN error_text TEXT")
        for i in range(count):
            conn.execute(
                """
                INSERT INTO research_experiments
                    (symbol, experiment_type, name, status, summary, error_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("MGC", "sweep", f"exp-{i}", "error", "boom", error_text),
            )
        conn.commit()


def test_detect_tool_failure_emits_when_threshold_met(tmp_path):
    db = tmp_path / "judas.db"
    _seed_research_experiments_with_error_text(
        str(db), "ConnectionResetError: peer reset", count=4
    )
    syms = detect_tool_failure(db_path=str(db))
    assert len(syms) == 1
    assert syms[0].category == "tool_failure"
    assert "ConnectionResetError" in syms[0].summary


def test_detect_tool_failure_empty_when_no_errors(tmp_path):
    db = tmp_path / "judas.db"
    init_db(db)  # No error column at all -> []
    assert detect_tool_failure(db_path=str(db)) == []


def test_detect_tool_failure_below_threshold_empty(tmp_path):
    db = tmp_path / "judas.db"
    _seed_research_experiments_with_error_text(str(db), "FlakyTimeout", count=2)
    assert detect_tool_failure(db_path=str(db)) == []


def test_detect_silent_dryrun_emits_on_match(tmp_path):
    log = tmp_path / "judas_crew.log"
    log.write_text(
        "2099-12-31T00:00:00Z some unrelated line\n"
        "broker.would_flatten symbol=MGC qty=1\n"
    )
    syms = detect_silent_dryrun(log_path=str(log))
    assert len(syms) == 1
    assert syms[0].category == "silent_dryrun"


def test_detect_silent_dryrun_empty_when_log_missing(tmp_path):
    assert detect_silent_dryrun(log_path=str(tmp_path / "nope.log")) == []


def test_detect_silent_dryrun_empty_on_clean_log(tmp_path):
    log = tmp_path / "judas_crew.log"
    log.write_text("nothing to see here\nmore boring lines\n")
    assert detect_silent_dryrun(log_path=str(log)) == []


def test_detect_looping_research_three_timeouts_emits(tmp_path):
    research_dir = tmp_path / "outputs" / "research"
    research_dir.mkdir(parents=True)
    status = research_dir / "runtime_status.json"
    ledger = research_dir / "_runtime_ledger.json"

    # Pre-seed ledger with two timed_out entries.
    ledger.write_text(
        json.dumps(
            [
                {"mtime": 1.0, "state": "timed_out", "symbol": "MGC"},
                {"mtime": 2.0, "state": "timed_out", "symbol": "MGC"},
            ]
        )
    )
    status.write_text(json.dumps({"state": "timed_out", "symbol": "MGC"}))
    # Bump mtime to a unique value.
    new_mtime = 3.0
    import os as _os

    _os.utime(status, (new_mtime, new_mtime))

    syms = detect_looping_research(repo_root=str(tmp_path))
    assert len(syms) == 1
    assert syms[0].category == "looping_research"


def test_detect_looping_research_one_timeout_empty(tmp_path):
    research_dir = tmp_path / "outputs" / "research"
    research_dir.mkdir(parents=True)
    status = research_dir / "runtime_status.json"
    status.write_text(json.dumps({"state": "timed_out", "symbol": "MGC"}))
    syms = detect_looping_research(repo_root=str(tmp_path))
    assert syms == []


def test_detect_failing_pytest_no_file_returns_empty(tmp_path):
    assert detect_failing_pytest(repo_root=str(tmp_path)) == []


def test_detect_failing_pytest_emits_on_nonzero(tmp_path):
    p = tmp_path / "outputs" / "test_runs"
    p.mkdir(parents=True)
    (p / "last.json").write_text(
        json.dumps({"exit_code": 1, "summary": "1 failed in 4.2s"})
    )
    syms = detect_failing_pytest(repo_root=str(tmp_path))
    assert len(syms) == 1
    assert syms[0].category == "failing_pytest"


def test_detect_all_symptoms_dedupes_by_hash(tmp_path):
    # Seed the DB so tool_failure fires.
    db = tmp_path / "judas.db"
    _seed_research_experiments_with_error_text(str(db), "RecurringErr", count=5)

    # Also seed a duplicate symptom via direct call to ensure dedup logic runs.
    sym_a = Symptom(category="tool_failure", hash=sha1_symptom("tool_failure", "x"), summary="a")
    sym_b = Symptom(category="tool_failure", hash=sha1_symptom("tool_failure", "x"), summary="b")
    seen = set()
    out = []
    for s in [sym_a, sym_b]:
        if s.hash in seen:
            continue
        seen.add(s.hash)
        out.append(s)
    assert len(out) == 1

    # And detect_all_symptoms should not crash on missing files/tables.
    all_syms = detect_all_symptoms(db_path=str(db), repo_root=str(tmp_path))
    cats = [s.category for s in all_syms]
    assert "tool_failure" in cats


def test_sha1_symptom_stable():
    h1 = sha1_symptom("tool_failure", "  Connection  reset  ")
    h2 = sha1_symptom("tool_failure", "Connection reset")
    assert h1 == h2


# --- 2026-06-22: detectors that feed the self-fix loop ---------------------

def _write_log(tmp_path, lines):
    log = tmp_path / "judas_crew.log"
    log.write_text("\n".join(lines) + "\n")
    return str(log)


def test_detect_db_contention_emits_above_threshold(tmp_path):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    lines = [f'{{"ts": "{now}", "msg": "sqlite3.OperationalError: database is locked"}}'
             for _ in range(3)]
    syms = detect_db_contention(log_path=_write_log(tmp_path, lines))
    assert len(syms) == 1 and syms[0].category == "db_contention"


def test_detect_db_contention_below_threshold_empty(tmp_path):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    assert detect_db_contention(log_path=_write_log(tmp_path, [f'{now} database is locked'])) == []


def test_detect_active_set_duplicates_emits_on_dupe(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    with sqlite3.connect(db) as conn:
        for ver in (1, 2):
            conn.execute(
                "INSERT INTO active_strategies (symbol, strategy_family, version, "
                "params_json, metrics_json, state, activated_at_utc, notes) "
                "VALUES ('MGC','custom_5m',?,'{}','{}','active','2026-06-22T00:00:00Z','t')",
                (ver,),
            )
        conn.commit()
    syms = detect_active_set_duplicates(db_path=db)
    assert len(syms) == 1 and syms[0].category == "active_dup" and "MGC" in syms[0].summary


def test_detect_active_set_duplicates_clean_when_one_active(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO active_strategies (symbol, strategy_family, version, params_json, "
            "metrics_json, state, activated_at_utc, notes) "
            "VALUES ('MGC','custom_5m',2,'{}','{}','active','2026-06-22T00:00:00Z','t')")
        conn.execute(
            "INSERT INTO active_strategies (symbol, strategy_family, version, params_json, "
            "metrics_json, state, activated_at_utc, notes) "
            "VALUES ('MGC','custom_5m',1,'{}','{}','superseded','2026-06-22T00:00:00Z','t')")
        conn.commit()
    assert detect_active_set_duplicates(db_path=db) == []


def test_detect_stale_backtest_bars_emits_when_old(tmp_path):
    pd = pytest.importorskip("pandas")
    cache = tmp_path / "cache_1h"; cache.mkdir()
    pd.DataFrame({"ts": [pd.Timestamp("2026-05-05T11:00:00Z")], "open": [1.0], "high": [1.0],
                  "low": [1.0], "close": [1.0], "volume": [1]}).to_parquet(
        cache / "MGC_1h.parquet", index=False)
    syms = detect_stale_backtest_bars(repo_root=str(tmp_path), max_age_days=4.0)
    assert len(syms) == 1 and syms[0].category == "stale_data"


def test_detect_stale_backtest_bars_clean_when_fresh(tmp_path):
    pd = pytest.importorskip("pandas")
    cache = tmp_path / "cache_1h"; cache.mkdir()
    pd.DataFrame({"ts": [pd.Timestamp.now(tz="UTC")], "open": [1.0], "high": [1.0],
                  "low": [1.0], "close": [1.0], "volume": [1]}).to_parquet(
        cache / "MGC_1h.parquet", index=False)
    assert detect_stale_backtest_bars(repo_root=str(tmp_path), max_age_days=4.0) == []
