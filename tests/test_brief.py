"""Phase 4 tests for src.research.brief and the OperatorFlow wiring."""
from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.db.models import init_db, get_conn  # noqa: E402
from src.research import brief as brief_mod  # noqa: E402


NY_TZ = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_strategy(
    db_path: Path,
    *,
    name: str = "judas_base_1h",
    symbol: str = "MGC",
    family: str = "judas_1h",
    backtest: dict | None = None,
) -> int:
    init_db(str(db_path))
    params: dict = {"strategy_name": name, "symbol": symbol}
    if backtest is not None:
        params["backtest"] = backtest
    with get_conn(str(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES (?, ?, 1, ?, '{}', 'active', ?, 'seed')
            """,
            (
                symbol,
                family,
                json.dumps(params),
                _utc_iso(datetime.now(timezone.utc)),
            ),
        )
        return int(cur.lastrowid)


def _seed_trade(
    db_path: Path,
    *,
    strategy_id: int,
    pnl: float,
    opened_utc: datetime,
    status: str = "closed",
    symbol: str = "MGC",
) -> None:
    with get_conn(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO trades
                (signal_id, strategy_id, symbol, direction, qty, entry_fill,
                 pnl_dollars, status, opened_at, closed_at)
            VALUES (NULL, ?, ?, 'long', 1, 100.0, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                symbol,
                float(pnl),
                status,
                _utc_iso(opened_utc),
                _utc_iso(opened_utc + timedelta(minutes=30)) if status == "closed" else None,
            ),
        )


def _et_noon(brief_date: str) -> datetime:
    """Return noon ET on the given YYYY-MM-DD as a UTC datetime."""
    parts = [int(x) for x in brief_date.split("-")]
    return datetime(parts[0], parts[1], parts[2], 12, 0, 0, tzinfo=NY_TZ).astimezone(
        timezone.utc
    )


# ---------------------------------------------------------------------------
# compose_daily_brief
# ---------------------------------------------------------------------------


def test_compose_brief_with_seeded_trades(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(tmp_path / "briefs"))
    db = tmp_path / "judas_crew.db"
    sid_a = _seed_strategy(db, name="alpha")
    sid_b = _seed_strategy(db, name="beta")

    brief_date = "2026-05-08"
    et_noon = _et_noon(brief_date)
    _seed_trade(db, strategy_id=sid_a, pnl=120.0, opened_utc=et_noon)
    _seed_trade(db, strategy_id=sid_a, pnl=-50.0, opened_utc=et_noon + timedelta(hours=1))
    _seed_trade(db, strategy_id=sid_a, pnl=30.0, opened_utc=et_noon + timedelta(hours=2))
    _seed_trade(db, strategy_id=sid_b, pnl=-25.0, opened_utc=et_noon + timedelta(hours=3))
    _seed_trade(db, strategy_id=sid_b, pnl=75.0, opened_utc=et_noon + timedelta(hours=4))

    result = brief_mod.compose_daily_brief(db_path=str(db), brief_date=brief_date)
    summary = result["summary"]

    assert summary["fills"] == 5
    assert summary["pnl_total"] == pytest.approx(150.0)
    by_strategy = {e["strategy_id"]: e for e in summary["pnl_by_strategy"]}
    assert by_strategy[sid_a]["pnl"] == pytest.approx(100.0)
    assert by_strategy[sid_a]["trades"] == 3
    assert by_strategy[sid_b]["pnl"] == pytest.approx(50.0)
    assert by_strategy[sid_b]["trades"] == 2
    assert "regime" in summary and {"vol_regime", "trend", "leaders"} <= set(
        summary["regime"].keys()
    )
    assert isinstance(summary["surprises"], list)
    assert isinstance(summary["recommended_actions"], list)
    assert "# Daily Brief" in result["content_md"]


def test_compose_brief_no_trades(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(tmp_path / "briefs"))
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    result = brief_mod.compose_daily_brief(db_path=str(db), brief_date="2026-05-08")
    summary = result["summary"]
    assert summary["fires"] == 0
    assert summary["fills"] == 0
    assert summary["pnl_total"] == 0.0
    assert summary["pnl_by_strategy"] == []
    assert summary["surprises"] == []


# ---------------------------------------------------------------------------
# persist_daily_brief
# ---------------------------------------------------------------------------


def test_persist_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(tmp_path / "briefs"))
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    summary = {"fires": 1, "fills": 1, "pnl_total": 42.0}
    bid = brief_mod.persist_daily_brief(
        db_path=str(db),
        brief_date="2026-05-08",
        content_md="# hi",
        summary_json=summary,
    )
    assert bid > 0

    with get_conn(str(db)) as conn:
        row = conn.execute(
            "SELECT brief_date, content_md, summary_json FROM daily_briefs WHERE id = ?",
            (bid,),
        ).fetchone()
    assert row["brief_date"] == "2026-05-08"
    assert row["content_md"] == "# hi"
    assert json.loads(row["summary_json"])["pnl_total"] == 42.0


def test_persist_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(tmp_path / "briefs"))
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    bid_1 = brief_mod.persist_daily_brief(
        db_path=str(db),
        brief_date="2026-05-08",
        content_md="# v1",
        summary_json={"v": 1},
    )
    bid_2 = brief_mod.persist_daily_brief(
        db_path=str(db),
        brief_date="2026-05-08",
        content_md="# v2",
        summary_json={"v": 2},
    )
    # Same brief_date — must overwrite, not duplicate.
    with get_conn(str(db)) as conn:
        rows = conn.execute(
            "SELECT id, content_md FROM daily_briefs WHERE brief_date = ?",
            ("2026-05-08",),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["content_md"] == "# v2"


def test_brief_writes_markdown_file(tmp_path, monkeypatch):
    briefs_dir = tmp_path / "briefs"
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(briefs_dir))
    db = tmp_path / "judas_crew.db"
    init_db(str(db))

    brief_mod.persist_daily_brief(
        db_path=str(db),
        brief_date="2026-05-08",
        content_md="# hi",
        summary_json={"pnl_total": 0.0},
    )
    assert (briefs_dir / "2026-05-08.md").exists()
    assert (briefs_dir / "2026-05-08.md").read_text() == "# hi"


# ---------------------------------------------------------------------------
# Recommendations: demotions in last 24h
# ---------------------------------------------------------------------------


def test_brief_includes_demotion_recommendations(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(tmp_path / "briefs"))
    db = tmp_path / "judas_crew.db"
    sid = _seed_strategy(db, name="rip")

    # Auto-demotion row from 2 hours ago.
    recent_ts = _utc_iso(datetime.now(timezone.utc) - timedelta(hours=2))
    with get_conn(str(db)) as conn:
        conn.execute(
            """
            INSERT INTO auto_demotions
                (ts_utc, strategy_id, symbol, strategy_family, version,
                 params_json, metrics_snapshot_json, reason)
            VALUES (?, ?, 'MGC', 'judas_1h', 1, '{}', '{}', 'pf_20 below 0.9')
            """,
            (recent_ts, sid),
        )

    brief_date = "2026-05-08"
    result = brief_mod.compose_daily_brief(db_path=str(db), brief_date=brief_date)
    actions = result["summary"]["recommended_actions"]
    retire_actions = [a for a in actions if a["type"] == "retire"]
    assert any(a["strategy_id"] == sid for a in retire_actions)
    assert all("demotion_id" in a for a in retire_actions)


# ---------------------------------------------------------------------------
# OperatorFlow integration
# ---------------------------------------------------------------------------


crewai = pytest.importorskip("crewai")
_crewai_version = tuple(int(p) for p in crewai.__version__.split(".")[:2])


@pytest.mark.skipif(
    _crewai_version < (1, 8),
    reason=f"OperatorFlow requires crewai >= 1.8, found {crewai.__version__}",
)
def test_write_brief_step_via_flow(tmp_path, monkeypatch):
    """Run OperatorFlow with morning_review forced to noop; assert daily_briefs row."""
    state_db = tmp_path / "flow_state.db"
    judas_db = tmp_path / "judas_crew.db"
    briefs_dir = tmp_path / "briefs"

    monkeypatch.setenv("JUDAS_OPERATOR_STATE_DB", str(state_db))
    monkeypatch.setenv("JUDAS_DB_PATH", str(judas_db))
    monkeypatch.setenv("JUDAS_BRIEFS_DIR", str(briefs_dir))

    init_db(str(judas_db))

    # Drop cached module so @persist re-evaluates with the new env var.
    for name in list(sys.modules):
        if name == "src.flows.operator_flow" or name.startswith(
            "src.flows.operator_flow."
        ):
            del sys.modules[name]
    of = importlib.import_module("src.flows.operator_flow")
    # Force noop routing so the Phase 5 explore triggers don't flip this
    # test's expectation of decision == "noop".
    monkeypatch.setattr(of, "_decide_explore_or_noop", lambda *, db_path: ("noop", None))

    # Force morning_review to return "noop" path (no active strategies seeded
    # so review_all_active_strategies returns []).
    flow = of.OperatorFlow()
    flow.kickoff(inputs={"id": of.OPERATOR_FLOW_ID})

    assert flow.state.decision == "noop"

    # Yesterday in ET.
    yesterday_et = (datetime.now(NY_TZ).date() - timedelta(days=1)).isoformat()
    with get_conn(str(judas_db)) as conn:
        rows = conn.execute(
            "SELECT brief_date FROM daily_briefs ORDER BY id DESC"
        ).fetchall()
    assert any(r["brief_date"] == yesterday_et for r in rows), (
        f"expected daily_briefs row for {yesterday_et}; got {[dict(r) for r in rows]}"
    )
