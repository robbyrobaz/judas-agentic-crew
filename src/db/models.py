"""SQLite schema and helpers for judas-agentic-crew.

Two tables:
  signals — every detected+evaluated Judas setup
  trades  — every executed paper order, linked to a signal

Usage:
    from src.db.models import init_db, get_conn
    init_db("judas_crew.db")
    with get_conn("judas_crew.db") as conn:
        ...
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc          TEXT    NOT NULL,          -- ISO-8601 UTC timestamp of bar that triggered
    symbol          TEXT    NOT NULL,
    strategy_id     INTEGER,
    strategy_family TEXT,
    strategy_version INTEGER,
    direction       TEXT    NOT NULL,          -- "long" | "short"
    quality_score   REAL,                      -- 0-10 from SetupEvaluator
    risk_decision   TEXT,                      -- "TRADE" | "SKIP"
    entry           REAL,
    stop            REAL,
    target          REAL,
    rationale       TEXT,                      -- deterministic detector rationale
    agent_notes     TEXT,                      -- JSON blob of full agent reasoning chain
    raw_llm_output  TEXT,                      -- raw CrewAI task outputs (for debug/replay)
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id       INTEGER REFERENCES signals(id),
    strategy_id     INTEGER,
    strategy_family TEXT,
    strategy_version INTEGER,
    ibkr_order_id   TEXT,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,          -- "long" | "short"
    qty             INTEGER NOT NULL DEFAULT 1,
    entry_fill      REAL,                      -- actual fill price
    stop_price      REAL,
    target_price    REAL,
    exit_fill       REAL,
    pnl_dollars     REAL,
    status          TEXT    NOT NULL DEFAULT 'open',  -- "open" | "closed" | "cancelled"
    opened_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    closed_at       TEXT
);
"""

_CREATE_RESEARCH_EXPERIMENTS = """
CREATE TABLE IF NOT EXISTS research_experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    symbol              TEXT    NOT NULL,
    experiment_type     TEXT    NOT NULL,
    name                TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'completed',
    metrics_json        TEXT,
    parameters_json     TEXT,
    artifacts_json      TEXT,
    summary             TEXT,
    recommendations_json TEXT
);
"""

_CREATE_STRATEGY_CANDIDATES = """
CREATE TABLE IF NOT EXISTS strategy_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    symbol              TEXT    NOT NULL,
    strategy_family     TEXT    NOT NULL,
    source_experiment_id INTEGER,
    params_json         TEXT    NOT NULL,
    metrics_json        TEXT    NOT NULL,
    decision            TEXT    NOT NULL,
    rationale           TEXT,
    status              TEXT    NOT NULL DEFAULT 'candidate'
);
"""

_CREATE_ACTIVE_STRATEGIES = """
CREATE TABLE IF NOT EXISTS active_strategies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT    NOT NULL,
    strategy_family     TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    params_json         TEXT    NOT NULL,
    metrics_json        TEXT,
    source_candidate_id INTEGER,
    state               TEXT    NOT NULL DEFAULT 'active',
    activated_at_utc    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    deactivated_at_utc  TEXT,
    notes               TEXT
);
"""

_CREATE_AUTO_DEMOTIONS = """
CREATE TABLE IF NOT EXISTS auto_demotions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc                   TEXT    NOT NULL,
    strategy_id              INTEGER NOT NULL,
    symbol                   TEXT    NOT NULL,
    strategy_family          TEXT    NOT NULL,
    version                  INTEGER NOT NULL,
    params_json              TEXT    NOT NULL,
    metrics_snapshot_json    TEXT    NOT NULL,
    reason                   TEXT    NOT NULL,
    reactivated_at_utc       TEXT,
    reactivated_strategy_id  INTEGER
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_signals_ts_utc  ON signals(ts_utc);",
    "CREATE INDEX IF NOT EXISTS ix_signals_symbol  ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_trades_signal   ON trades(signal_id);",
    "CREATE INDEX IF NOT EXISTS ix_trades_status   ON trades(status);",
    "CREATE INDEX IF NOT EXISTS ix_trades_opened   ON trades(opened_at);",
    "CREATE INDEX IF NOT EXISTS ix_research_symbol ON research_experiments(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_research_type   ON research_experiments(experiment_type);",
    "CREATE INDEX IF NOT EXISTS ix_research_ts     ON research_experiments(ts_utc);",
    "CREATE INDEX IF NOT EXISTS ix_candidates_symbol ON strategy_candidates(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_candidates_family ON strategy_candidates(strategy_family);",
    "CREATE INDEX IF NOT EXISTS ix_candidates_status ON strategy_candidates(status);",
    "CREATE INDEX IF NOT EXISTS ix_active_symbol ON active_strategies(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_active_family ON active_strategies(strategy_family);",
    "CREATE INDEX IF NOT EXISTS ix_active_state ON active_strategies(state);",
    "CREATE INDEX IF NOT EXISTS ix_demotions_ts ON auto_demotions(ts_utc);",
    "CREATE INDEX IF NOT EXISTS ix_demotions_strategy ON auto_demotions(strategy_id);",
    "CREATE INDEX IF NOT EXISTS ix_demotions_symbol ON auto_demotions(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_signals_strategy ON signals(strategy_id, strategy_family, strategy_version);",
    "CREATE INDEX IF NOT EXISTS ix_trades_strategy ON trades(strategy_id, strategy_family, strategy_version);",
]

_MIGRATIONS = [
    ("signals", "strategy_id", "ALTER TABLE signals ADD COLUMN strategy_id INTEGER"),
    ("signals", "strategy_family", "ALTER TABLE signals ADD COLUMN strategy_family TEXT"),
    ("signals", "strategy_version", "ALTER TABLE signals ADD COLUMN strategy_version INTEGER"),
    ("trades", "strategy_id", "ALTER TABLE trades ADD COLUMN strategy_id INTEGER"),
    ("trades", "strategy_family", "ALTER TABLE trades ADD COLUMN strategy_family TEXT"),
    ("trades", "strategy_version", "ALTER TABLE trades ADD COLUMN strategy_version INTEGER"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def init_db(db_path: str | Path) -> None:
    """Create tables and indexes if they do not exist."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(_CREATE_SIGNALS)
        conn.execute(_CREATE_TRADES)
        conn.execute(_CREATE_RESEARCH_EXPERIMENTS)
        conn.execute(_CREATE_STRATEGY_CANDIDATES)
        conn.execute(_CREATE_ACTIVE_STRATEGIES)
        conn.execute(_CREATE_AUTO_DEMOTIONS)
        for table, column, ddl in _MIGRATIONS:
            if not _column_exists(conn, table, column):
                conn.execute(ddl)
        for idx in _INDEXES:
            conn.execute(idx)
        conn.commit()


@contextmanager
def get_conn(db_path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """Context-manager that yields a sqlite3 connection with WAL + FK enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
