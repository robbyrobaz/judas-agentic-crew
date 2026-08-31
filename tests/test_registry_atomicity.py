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


def test_insert_active_strategy_custom_different_csids_coexist(db_path):
    """Regression (2026-07-05): for the `custom` family, the slot key is the
    custom_strategy_id, not the family. Different csids load different code
    (different architectures), so multiple customs on the same symbol MUST
    coexist. Before the fix, promoting a new iFVG variant on a non-banned
    symbol would supersede an existing ATR-disp variant on the same symbol —
    loss of architectural diversity the brief explicitly allows.

    NOTE (2026-08-18): original test used MBT, but MBT is in
    lucid_guard.banned_symbols = {"MET", "MBT", "DX"} (see
    src/research/lucid_guard.py RULES["banned_symbols"]) and is now rejected
    at insert time by _validate_lucid_ban. Switched to MCL — the architectural
    coexistence logic is symbol-agnostic; MCL is non-banned and has a
    similar multi-active slot profile in production."""
    from src import strategy_registry as sr

    test_symbol = "MCL"  # non-banned; was MBT pre-2026-08-18

    # Insert two distinct test custom_strategy_id rows so the test doesn't
    # depend on specific production csids.
    conn = sqlite3.connect(sr._db_path())
    cur = conn.execute(
        "INSERT INTO custom_strategies (name, code, active, created_at_utc, symbol, rationale, backtest_metrics_json) VALUES (?, ?, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, 'test stub', '{}')",
        ("test_slotkey_csid_a", "def evaluate(bars, params): return []", test_symbol),
    )
    csid_a = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO custom_strategies (name, code, active, created_at_utc, symbol, rationale, backtest_metrics_json) VALUES (?, ?, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, 'test stub', '{}')",
        ("test_slotkey_csid_b", "def evaluate(bars, params): return []", test_symbol),
    )
    csid_b = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        # Two distinct customs on the symbol — different csids. Both must stay active.
        sr.insert_active_strategy(
            symbol=test_symbol, strategy_family="custom",
            params={"symbol": test_symbol, "strategy_family": "custom",
                    "execution_engine": "custom", "custom_strategy_id": csid_a,
                    "timeframe": "5m"},
        )
        sr.insert_active_strategy(
            symbol=test_symbol, strategy_family="custom",
            params={"symbol": test_symbol, "strategy_family": "custom",
                    "execution_engine": "custom", "custom_strategy_id": csid_b,
                    "timeframe": "5m"},
        )

        actives = [r for r in sr.list_active_strategies()
                   if r["symbol"] == test_symbol and r["strategy_family"] == "custom"
                   and r["state"] == "active"]
        active_csids = sorted(
            r["params"].get("custom_strategy_id") for r in actives
        )
        assert active_csids == sorted([csid_a, csid_b]), (
            f"expected both csids active, got {active_csids}"
        )

        # A THIRD insert with the SAME csid as the first should supersede
        # ONLY the first row — the second (different csid) must stay active.
        sr.insert_active_strategy(
            symbol=test_symbol, strategy_family="custom",
            params={"symbol": test_symbol, "strategy_family": "custom",
                    "execution_engine": "custom", "custom_strategy_id": csid_a,
                    "timeframe": "5m"},
        )
        actives = [r for r in sr.list_active_strategies()
                   if r["symbol"] == test_symbol and r["strategy_family"] == "custom"
                   and r["state"] == "active"]
        active_csids = sorted(
            r["params"].get("custom_strategy_id") for r in actives
        )
        assert active_csids == sorted([csid_a, csid_b]), (
            f"same-csid re-insert should supersede prior a but leave b active; got {active_csids}"
        )
    finally:
        conn = sqlite3.connect(sr._db_path())
        conn.execute("DELETE FROM custom_strategies WHERE id IN (?, ?)",
                     (csid_a, csid_b))
        conn.commit()
        conn.close()


def test_insert_active_strategy_supersedes_string_typed_csid(db_path):
    """Regression (2026-07-28): JSON_EXTRACT preserves the JSON type of
    custom_strategy_id, so a row that stored it as a JSON string ("232")
    silently escaped the slot-aware supersede path (the compare bound was
    int). Pre-fix audit: 5 active rows had string CSIDs
    (4385/4434/4435/4472/4484) and a same-csid re-promotion would have
    spawned a parallel twin instead of superseding. The fix wraps the
    JSON_EXTRACT with CAST(... AS INTEGER) so both shapes coerce the same.
    """
    from src import strategy_registry as sr
    import sqlite3

    # Register a stub csid for this test.
    conn = sqlite3.connect(sr._db_path())
    cur = conn.execute(
        "INSERT INTO custom_strategies (name, code, active, created_at_utc, symbol, rationale, backtest_metrics_json) VALUES (?, ?, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'), 'MGC', 'test stub', '{}')",
        ("test_string_csid_stub", "def evaluate(bars, params): return []"),
    )
    csid = cur.lastrowid
    conn.commit()
    conn.close()

    try:
        # First insert — explicitly use a STRING-typed csid to mimic the
        # legacy params_json shape ("232") that older promotions emitted.
        params_str = {
            "symbol": "MGC", "strategy_family": "custom_5m",
            "execution_engine": "custom", "timeframe": "5m",
        }
        # Force the legacy string shape by round-tripping through json.dumps.
        import json
        params_str["custom_strategy_id"] = str(csid)  # e.g. "232"
        params_str = json.loads(json.dumps(params_str))  # ensure quoted-string shape

        a = sr.insert_active_strategy(
            symbol="MGC", strategy_family="custom_5m",
            params=params_str,
        )

        # Verify the CSID landed as a JSON STRING (the buggy shape).
        # list_active_strategies returns parsed params; check the raw json
        # storage directly so we see the actual JSON type.
        conn = sqlite3.connect(sr._db_path())
        row = conn.execute(
            "SELECT params_json FROM active_strategies WHERE id = ?", (a.id,)
        ).fetchone()
        conn.close()
        raw = json.loads(row[0])
        assert isinstance(raw["custom_strategy_id"], str), (
            f"pre-condition: csid must be stored as JSON string, got "
            f"{type(raw['custom_strategy_id']).__name__}"
        )

        # Re-promote with the SAME csid (int form this time, like all new
        # promotions). The slot-aware supersede MUST retire a and add the
        # new version — pre-fix this would have left BOTH active because
        # JSON_EXTRACT("232") != 232.
        params_int = dict(params_str)
        params_int["custom_strategy_id"] = csid  # int form
        params_int = json.loads(json.dumps(params_int))

        b = sr.insert_active_strategy(
            symbol="MGC", strategy_family="custom_5m",
            params=params_int,
        )

        actives = sr.list_active_strategies()
        active_csids = sorted(
            r["params"].get("custom_strategy_id") for r in actives
            if r["symbol"] == "MGC" and r["strategy_family"] == "custom_5m"
            and r["state"] == "active"
        )
        # After the fix, only b (int form) should be active for this csid.
        assert b.id != a.id
        assert active_csids == [csid], (
            f"string-csid row a={a.id} must be superseded by int-csid row "
            f"b={b.id}; got active_csids={active_csids}"
        )
    finally:
        conn = sqlite3.connect(sr._db_path())
        conn.execute("DELETE FROM custom_strategies WHERE id = ?", (csid,))
        conn.commit()
        conn.close()


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


def test_non_custom_engine_with_custom_strategy_id_is_zombie(db_path):
    """2026-08-30 guard: a row with execution_engine != 'custom' but
    custom_strategy_id set is a zombie (can never fire). The custom branch
    refuses (wrong engine); the judas/buffet branch refuses to load custom
    code. The runtime's zombie_skip silently drops it (per findings 712520f8
    + e18c0432). The registry-side validator must catch this at promotion
    time so the operator/researcher sees the error instead of an idle row.

    Regression for the 2026-08-30 #4598 MCL promotion bug: candidate was
    emitted with csid=156 + execution_engine='judas_native' (the registrar's
    default), but csid=156 is a custom row that only fires under
    execution_engine='custom'. The promotion succeeded; the row sits idle.
    """
    import pytest
    from src import strategy_registry as sr
    from src.db.models import get_conn

    # Seed a real custom_strategy row so csid=1 is valid (we only need the
    # engine/csid mismatch check, not the loadability check, to fire first).
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO custom_strategies (id, created_at_utc, name, symbol, "
            "code, rationale, backtest_metrics_json, active) "
            "VALUES (1, '2026-08-30T00:00:00Z', 'test_zombie_csid', 'MGC', "
            "'code', 'test', '{}', 1)"
        )

    with pytest.raises(ValueError, match="zombie combination"):
        sr.insert_active_strategy(
            symbol="MGC", strategy_family="atr_disp_continuation_5m",
            params={
                "execution_engine": "judas_native",
                "custom_strategy_id": 1,  # <-- the bug: csid set, wrong engine
                "strategy_name": "test_zombie",
            },
        )
