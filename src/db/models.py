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

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_signals_ts_utc  ON signals(ts_utc);",
    "CREATE INDEX IF NOT EXISTS ix_signals_symbol  ON signals(symbol);",
    "CREATE INDEX IF NOT EXISTS ix_trades_signal   ON trades(signal_id);",
    "CREATE INDEX IF NOT EXISTS ix_trades_status   ON trades(status);",
    "CREATE INDEX IF NOT EXISTS ix_trades_opened   ON trades(opened_at);",
]


def init_db(db_path: str | Path) -> None:
    """Create tables and indexes if they do not exist."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute(_CREATE_SIGNALS)
        conn.execute(_CREATE_TRADES)
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
