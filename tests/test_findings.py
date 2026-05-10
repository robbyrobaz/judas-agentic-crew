"""Coverage for shared findings memory + agent palette regressions."""
from __future__ import annotations

import json
import sqlite3

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


# ---------------------------------------------------------------------------
# record_finding
# ---------------------------------------------------------------------------


def test_record_finding_inserts_row(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    out = rec(title="MGC tends to sweep at 09:30 ET",
              body="Across the last 30 sessions, 60% of NY-open MGC sweeps "
                   "tagged the prior swing within the first 5 minutes.")
    assert out["ok"] is True
    fid = out["finding_id"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    assert row is not None
    assert row["author"] == "researcher"
    assert row["status"] == "active"
    assert "MGC" in row["title"]
    assert row["strategy_id"] is None
    assert row["refs_json"] is None


def test_record_finding_with_all_fields(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="operator")
    refs = {"experiment_id": 42, "candidate_id": 7}
    # Seed a prior finding to supersede.
    base = rec(title="initial", body="initial body")
    sup_id = base["finding_id"]
    out = rec(title="MGC ICT v2",
              body="Tightened threshold from 1.5 to 1.2 ATR.",
              strategy_id=58, strategy_name="judas_mgc_ict",
              symbol="MGC", refs=refs, supersedes_id=sup_id)
    assert out["ok"] is True
    assert out["superseded_id"] == sup_id
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        new_row = conn.execute(
            "SELECT * FROM findings WHERE id=?", (out["finding_id"],),
        ).fetchone()
    assert new_row["strategy_id"] == 58
    assert new_row["strategy_name"] == "judas_mgc_ict"
    assert new_row["symbol"] == "MGC"
    assert json.loads(new_row["refs_json"]) == refs
    assert new_row["supersedes_id"] == sup_id


def test_record_finding_supersedes_atomically(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    a = rec(title="early hypothesis", body="lorem")
    b = rec(title="updated hypothesis", body="ipsum",
            supersedes_id=a["finding_id"])
    assert b["ok"] is True and b["superseded_id"] == a["finding_id"]
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: r["status"] for r in conn.execute(
            "SELECT id, status FROM findings ORDER BY id"
        ).fetchall()}
    assert rows[a["finding_id"]] == "superseded"
    assert rows[b["finding_id"]] == "active"

    # Bad supersedes_id rolls back: no insert + the previous row's status
    # must remain whatever it currently is.
    bad = rec(title="will not insert", body="x", supersedes_id=99999)
    assert bad["ok"] is False
    with sqlite3.connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# read_findings
# ---------------------------------------------------------------------------


def _seed_findings(db: str, n: int, days_offset: int = 0,
                   strategy_id: int | None = None,
                   strategy_name: str | None = None,
                   symbol: str | None = None,
                   author: str = "researcher") -> None:
    """Direct insert with controlled created_at_utc (datetime modifiers)."""
    init_db(db)
    with sqlite3.connect(db) as conn:
        for i in range(n):
            conn.execute(
                """
                INSERT INTO findings
                  (created_at_utc, author, title, body, strategy_id,
                   strategy_name, symbol, status)
                VALUES
                  (datetime('now', ?), ?, ?, ?, ?, ?, ?, 'active')
                """,
                (f"-{days_offset} days", author,
                 f"title {i}", f"body {i}", strategy_id, strategy_name,
                 symbol),
            )
        conn.commit()


def test_read_findings_default_recent(tmp_path):
    db = str(tmp_path / "j.db")
    _seed_findings(db, n=15, days_offset=2)   # recent
    _seed_findings(db, n=15, days_offset=60)  # old
    read = make_read_findings(db_path=db)
    rows = read()
    # default since_days=30, default limit=20
    assert all("title" in r for r in rows)
    assert len(rows) == 15  # only the recent ones
    assert all(r["status"] == "active" for r in rows)


def test_read_findings_filter_by_strategy_id(tmp_path):
    db = str(tmp_path / "j.db")
    _seed_findings(db, n=3, days_offset=1, strategy_id=58)
    _seed_findings(db, n=4, days_offset=1, strategy_id=99)
    read = make_read_findings(db_path=db)
    rows = read(strategy_id=58)
    assert len(rows) == 3
    assert all(r["strategy_id"] == 58 for r in rows)


def test_read_findings_filter_by_strategy_name(tmp_path):
    db = str(tmp_path / "j.db")
    _seed_findings(db, n=2, days_offset=1, strategy_name="judas_mgc_ict")
    _seed_findings(db, n=5, days_offset=1, strategy_name="catalyst_mbt")
    read = make_read_findings(db_path=db)
    rows = read(strategy_name="catalyst_mbt")
    assert len(rows) == 5
    assert all(r["strategy_name"] == "catalyst_mbt" for r in rows)


def test_read_findings_keyword_search(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    rec(title="MGC sweep observation", body="banana split observation")
    rec(title="catalyst note", body="something else entirely")
    read = make_read_findings(db_path=db)
    rows = read(query="banana")
    assert len(rows) == 1
    assert "banana" in rows[0]["body"]
    rows = read(query="catalyst")
    assert len(rows) == 1
    assert rows[0]["title"] == "catalyst note"


def test_read_findings_status_filter(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    keep = rec(title="keep", body="alive")
    drop = rec(title="drop", body="alive")
    retr = make_retract_finding(db_path=db, author="operator")
    retr(id=drop["finding_id"], reason="bad data")
    read = make_read_findings(db_path=db)
    actives = read()
    ids = {r["id"] for r in actives}
    assert keep["finding_id"] in ids and drop["finding_id"] not in ids
    everything = read(include_status=["active", "retracted", "superseded"])
    assert {r["id"] for r in everything} >= {keep["finding_id"], drop["finding_id"]}


# ---------------------------------------------------------------------------
# retract_finding
# ---------------------------------------------------------------------------


def test_retract_finding_marks_status_and_appends_reason(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    rec = make_record_finding(db_path=db, author="researcher")
    out = rec(title="bad take", body="original body")
    retr = make_retract_finding(db_path=db, author="operator")
    res = retr(id=out["finding_id"], reason="contradicted by Q2 data")
    assert res["ok"] is True
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT body, status FROM findings WHERE id=?",
            (out["finding_id"],),
        ).fetchone()
    assert row["status"] == "retracted"
    assert row["body"].endswith("[retracted by operator: contradicted by Q2 data]")


# ---------------------------------------------------------------------------
# get_strategy_dossier
# ---------------------------------------------------------------------------


def test_get_strategy_dossier_returns_bundle(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    # Seed an active strategy row.
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
              (symbol, strategy_family, version, params_json, state)
            VALUES ('MGC', 'judas_mgc_ict', 1, '{}', 'active')
            """,
        )
        sid = int(cur.lastrowid)
        # Seed a demotion row (different strategy_id same family).
        conn.execute(
            """
            INSERT INTO auto_demotions
              (ts_utc, strategy_id, symbol, strategy_family, version,
               params_json, metrics_snapshot_json, reason)
            VALUES (datetime('now'), ?, 'MGC', 'judas_mgc_ict', 1,
                    '{}', '{}', 'drawdown breach')
            """,
            (sid,),
        )
        conn.commit()
    rec = make_record_finding(db_path=db, author="operator")
    rec(title="f1", body="b1", strategy_id=sid)
    rec(title="f2", body="b2", strategy_name="judas_mgc_ict")
    dossier = make_get_strategy_dossier(db_path=db)(strategy_id=sid)
    assert dossier["ok"] is True
    assert dossier["active"] is not None
    assert dossier["active"]["id"] == sid
    titles = {f["title"] for f in dossier["findings"]}
    assert {"f1", "f2"}.issubset(titles)
    assert len(dossier["demotions"]) == 1
    assert dossier["demotions"][0]["reason"] == "drawdown breach"


def test_get_strategy_dossier_by_name(tmp_path):
    db = str(tmp_path / "j.db")
    init_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO active_strategies
              (symbol, strategy_family, version, params_json, state)
            VALUES ('6J', 'catalyst_6j', 2, '{"x":1}', 'active')
            """,
        )
        # Add a candidate for the same family.
        conn.execute(
            """
            INSERT INTO strategy_candidates
              (symbol, strategy_family, params_json, metrics_json,
               decision, status)
            VALUES ('6J', 'catalyst_6j', '{}', '{}', 'review', 'candidate')
            """,
        )
        conn.commit()
    rec = make_record_finding(db_path=db, author="researcher")
    rec(title="f-by-name", body="b", strategy_name="catalyst_6j")
    dossier = make_get_strategy_dossier(db_path=db)(strategy_name="catalyst_6j")
    assert dossier["ok"] is True
    assert dossier["active"]["strategy_family"] == "catalyst_6j"
    assert any(f["title"] == "f-by-name" for f in dossier["findings"])
    assert len(dossier["candidates"]) == 1


# ---------------------------------------------------------------------------
# Palette regression — every team carries the four findings tools.
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


def test_make_tools_binds_findings_for_each_team(tmp_path):
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
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        authors = sorted(r["author"] for r in conn.execute(
            "SELECT author FROM findings ORDER BY id"
        ).fetchall())
    assert authors == ["coder", "registrar", "researcher", "trader"]
