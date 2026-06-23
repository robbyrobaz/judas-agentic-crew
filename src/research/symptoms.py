"""Symptom detectors for the Phase 3 code-fix delegation queue.

Each detector returns a list[Symptom]. Every detector must be defensive —
missing files, missing columns, missing logs must NOT raise; return [] instead.

Symptom categories (must match the auto_fixes.symptom_category contract):
  tool_failure | failing_pytest | looping_research | naked_position | silent_dryrun
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CATEGORIES = {
    "tool_failure",
    "failing_pytest",
    "looping_research",
    "naked_position",
    "silent_dryrun",
    # Added 2026-06-22 — three previously-silent classes now feed the self-fix loop:
    "db_contention",   # SQLite "database is locked" — a connect path needs ro/busy_timeout
    "active_dup",      # >1 active strategy per (symbol,family) → same-setup double-fire
    "stale_data",      # backtest bar cache gone stale → strategies validated on old data
}


@dataclass
class Symptom:
    category: str
    hash: str
    summary: str
    evidence: dict = field(default_factory=dict)


def sha1_symptom(category: str, normalized_evidence: str) -> str:
    """Stable hash for dedup. Truncates evidence to first 200 chars after norm."""
    norm = re.sub(r"\s+", " ", (normalized_evidence or "")).strip()[:200]
    h = hashlib.sha1()
    h.update(category.encode("utf-8"))
    h.update(b"|")
    h.update(norm.encode("utf-8"))
    return h.hexdigest()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return []
    return [row[1] for row in rows]


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def detect_tool_failure(*, db_path: str) -> list[Symptom]:
    """Same exception in research_experiments errors >=3 times in 24h.

    Reads either an `error_text` column or an `errors` column on
    research_experiments. If neither exists, returns [].
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    try:
        if not _has_table(conn, "research_experiments"):
            return []
        cols = _table_columns(conn, "research_experiments")
        err_col = None
        for candidate in ("error_text", "errors"):
            if candidate in cols:
                err_col = candidate
                break
        if err_col is None:
            return []

        ts_col = "ts_utc" if "ts_utc" in cols else None
        where_ts = ""
        if ts_col:
            where_ts = f" AND {ts_col} >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 days')"

        try:
            rows = conn.execute(
                f"SELECT {err_col} AS et FROM research_experiments "
                f"WHERE {err_col} IS NOT NULL AND {err_col} != ''{where_ts}"
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()

    # Group by normalized exception signature (first line / first 200 chars).
    counts: dict[str, list[str]] = {}
    for (et,) in rows:
        if not et:
            continue
        first_line = str(et).strip().splitlines()[0] if str(et).strip() else ""
        key = re.sub(r"\s+", " ", first_line)[:200]
        if not key:
            continue
        counts.setdefault(key, []).append(str(et))

    symptoms: list[Symptom] = []
    for key, examples in counts.items():
        if len(examples) >= 3:
            h = sha1_symptom("tool_failure", key)
            symptoms.append(
                Symptom(
                    category="tool_failure",
                    hash=h,
                    summary=f"Repeated tool failure ({len(examples)}x in 24h): {key}",
                    evidence={"signature": key, "count": len(examples)},
                )
            )
    return symptoms


def detect_failing_pytest(*, repo_root: str) -> list[Symptom]:
    """Looks for outputs/test_runs/last.json. If absent, returns []."""
    p = Path(repo_root) / "outputs" / "test_runs" / "last.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    exit_code = data.get("exit_code")
    if exit_code in (None, 0):
        return []
    summary = str(data.get("summary") or data.get("output_tail") or "pytest failed")[:200]
    h = sha1_symptom("failing_pytest", f"exit={exit_code} {summary}")
    return [
        Symptom(
            category="failing_pytest",
            hash=h,
            summary=f"pytest exited {exit_code}: {summary[:120]}",
            evidence={"exit_code": exit_code, "summary": summary},
        )
    ]


def detect_looping_research(*, repo_root: str) -> list[Symptom]:
    """Reads outputs/research/runtime_status.json plus a small ledger.

    Emits a symptom when the ledger shows 3 of the last 3 runs in state='timed_out'.
    The current runtime_status.json is appended to the ledger on each call (cheap).
    """
    status_path = Path(repo_root) / "outputs" / "research" / "runtime_status.json"
    ledger_path = Path(repo_root) / "outputs" / "research" / "_runtime_ledger.json"
    if not status_path.exists():
        return []
    try:
        cur = json.loads(status_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(cur, dict):
        return []

    state = cur.get("state")
    mtime = status_path.stat().st_mtime

    # Append to ledger if mtime advanced.
    ledger: list[dict[str, Any]] = []
    if ledger_path.exists():
        try:
            raw = json.loads(ledger_path.read_text())
            if isinstance(raw, list):
                ledger = raw
        except (json.JSONDecodeError, OSError):
            ledger = []

    if not ledger or ledger[-1].get("mtime") != mtime:
        ledger.append({"mtime": mtime, "state": state, "symbol": cur.get("symbol")})
        ledger = ledger[-10:]
        try:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(json.dumps(ledger))
        except OSError:
            pass

    last3 = ledger[-3:]
    if len(last3) < 3:
        return []
    if not all(entry.get("state") == "timed_out" for entry in last3):
        return []

    sig = "|".join(str(e.get("symbol") or "") for e in last3)
    h = sha1_symptom("looping_research", f"timed_out x3 {sig}")
    return [
        Symptom(
            category="looping_research",
            hash=h,
            summary="research runtime timed_out on 3 consecutive runs",
            evidence={"recent_states": last3},
        )
    ]


def detect_naked_position(*, db_path: str) -> list[Symptom]:
    """Naked-position drift. We do not connect to IBKR here.

    Best-effort: skip cleanly. If a `reconcile_log` table exists with rows
    flagging drift in the last 24h, emit; otherwise [].
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    try:
        if not _has_table(conn, "reconcile_log"):
            return []
        cols = _table_columns(conn, "reconcile_log")
        if "drift" not in cols and "naked" not in cols:
            return []
        col = "drift" if "drift" in cols else "naked"
        try:
            rows = conn.execute(
                f"SELECT symbol, {col} FROM reconcile_log "
                f"WHERE {col} = 1 "
                "AND ts_utc >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 days')"
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    if not rows:
        return []
    sig = ",".join(sorted({str(r[0]) for r in rows if r[0]}))
    h = sha1_symptom("naked_position", f"naked {sig}")
    return [
        Symptom(
            category="naked_position",
            hash=h,
            summary=f"naked-position drift on: {sig}",
            evidence={"symbols": sig.split(",") if sig else []},
        )
    ]


_DRYRUN_PATTERN = re.compile(r"broker\.would_[a-z_]+", re.IGNORECASE)


def detect_silent_dryrun(*, log_path: str = "logs/judas_crew.log") -> list[Symptom]:
    """Greps the log file for 'broker.would_*' lines emitted within last 24h.

    If the file is missing or unreadable, returns [].
    """
    p = Path(log_path)
    if not p.exists():
        return []
    try:
        # Bound read to last ~256KB to avoid OOM on huge logs.
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > 256 * 1024:
                fh.seek(-256 * 1024, 2)
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    hits: list[str] = []
    for line in blob.splitlines():
        m = _DRYRUN_PATTERN.search(line)
        if not m:
            continue
        # Try to extract an ISO timestamp; if not, accept the line.
        ts_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line)
        if ts_match:
            try:
                t = datetime.strptime(ts_match.group(0), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                if t.timestamp() < cutoff:
                    continue
            except ValueError:
                pass
        hits.append(line.strip())

    if not hits:
        return []

    sig = hits[0][:200]
    h = sha1_symptom("silent_dryrun", sig)
    return [
        Symptom(
            category="silent_dryrun",
            hash=h,
            summary=f"silent dry-run: {len(hits)} broker.would_* line(s) in last 24h",
            evidence={"count": len(hits), "first": hits[0][:200]},
        )
    ]


def detect_db_contention(*, log_path: str = "logs/judas_crew.log") -> list[Symptom]:
    """SQLite "database is locked" errors in the log over the last 24h.

    A lock error means a writer gave up — a lost signal/trade/registry write.
    The fixable root is usually a code path: a connect that should be read-only,
    or a missing busy_timeout. >=3 in 24h is a real pattern.
    """
    p = Path(log_path)
    if not p.exists():
        return []
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > 512 * 1024:
                fh.seek(-512 * 1024, 2)
            blob = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    hits: list[str] = []
    for line in blob.splitlines():
        if "database is locked" not in line.lower():
            continue
        ts_match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", line)
        if ts_match:
            try:
                t = datetime.strptime(ts_match.group(0), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                if t.timestamp() < cutoff:
                    continue
            except ValueError:
                pass
        hits.append(line.strip())

    if len(hits) < 3:
        return []
    h = sha1_symptom("db_contention", "database is locked")
    return [
        Symptom(
            category="db_contention",
            hash=h,
            summary=(
                f"SQLite 'database is locked' x{len(hits)} in 24h — a writer is losing "
                f"writes under contention. Find the offending connect site (a read path "
                f"opening read-write, or one missing PRAGMA busy_timeout) and fix it."
            ),
            evidence={"count": len(hits), "first": hits[0][:200]},
        )
    ]


def detect_active_set_duplicates(*, db_path: str) -> list[Symptom]:
    """Two+ active strategies sharing the same (symbol, strategy_family).

    They fire the same setup on the same bar — 2-3x size and loss, not
    diversification (observed live 2026-06-17). promote_candidate /
    insert_active_strategy supersede the prior version atomically; a group here
    means that invariant regressed and the coder should re-assert it.
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return []
    try:
        if not _has_table(conn, "active_strategies"):
            return []
        try:
            rows = conn.execute(
                """
                SELECT symbol, strategy_family, COUNT(*) AS n,
                       GROUP_CONCAT(version) AS versions
                FROM active_strategies
                WHERE state='active'
                GROUP BY symbol, strategy_family
                HAVING n > 1
                """
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()
    if not rows:
        return []
    groups = [f"{r[0]}/{r[1]} (versions {r[3]})" for r in rows]
    sig = ",".join(sorted(f"{r[0]}/{r[1]}" for r in rows))
    h = sha1_symptom("active_dup", sig)
    return [
        Symptom(
            category="active_dup",
            hash=h,
            summary=(
                f"{len(rows)} (symbol,family) group(s) have >1 ACTIVE strategy → same-setup "
                f"double-fire: {'; '.join(groups[:8])}. promote_candidate/insert_active_strategy "
                f"must atomically supersede the prior active version of the same (symbol,family)."
            ),
            evidence={"groups": groups},
        )
    ]


def detect_stale_backtest_bars(*, repo_root: str, max_age_days: float = 4.0) -> list[Symptom]:
    """Newest bar in THIS repo's 1h cache is older than max_age_days.

    The researcher backtests on cache_1h/. If it goes stale, strategies are
    validated on data that never saw recent regime — then lose live. 4-day
    default is weekend/holiday-safe.
    """
    cache_dir = Path(repo_root) / "cache_1h"
    if not cache_dir.exists():
        return []
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001
        return []
    newest = None
    checked = 0
    for p in sorted(cache_dir.glob("*_1h.parquet")):
        try:
            df = pd.read_parquet(p, columns=["ts"])
            if df.empty:
                continue
            ts = pd.to_datetime(df["ts"], utc=True).max()
            checked += 1
            if newest is None or ts > newest:
                newest = ts
        except Exception:  # noqa: BLE001
            continue
    if newest is None or checked == 0:
        return []
    age_days = (datetime.now(timezone.utc) - newest.to_pydatetime()).total_seconds() / 86400.0
    if age_days < max_age_days:
        return []
    h = sha1_symptom("stale_data", f"stale_1h_bars>{max_age_days}d")
    return [
        Symptom(
            category="stale_data",
            hash=h,
            summary=(
                f"Backtest bar cache is {age_days:.1f} days stale (newest 1h bar "
                f"{str(newest)[:10]}). The researcher is validating strategies on old data. "
                f"Check the bar-cache refresh path and that _load_bars reads THIS repo's "
                f"cache_1h, not a stale external one."
            ),
            evidence={"age_days": round(age_days, 1), "newest": str(newest), "checked": checked},
        )
    ]


def detect_all_symptoms(*, db_path: str, repo_root: str) -> list[Symptom]:
    """Run all detectors, dedupe by hash, return flat list."""
    out: list[Symptom] = []
    seen: set[str] = set()

    detectors = [
        lambda: detect_tool_failure(db_path=db_path),
        lambda: detect_failing_pytest(repo_root=repo_root),
        lambda: detect_looping_research(repo_root=repo_root),
        lambda: detect_naked_position(db_path=db_path),
        lambda: detect_silent_dryrun(log_path=str(Path(repo_root) / "logs" / "judas_crew.log")),
        # Added 2026-06-22 — three previously-silent classes now self-fix via the loop.
        lambda: detect_db_contention(log_path=str(Path(repo_root) / "logs" / "judas_crew.log")),
        lambda: detect_active_set_duplicates(db_path=db_path),
        lambda: detect_stale_backtest_bars(repo_root=repo_root),
    ]
    for fn in detectors:
        try:
            for sym in fn() or []:
                if sym.hash in seen:
                    continue
                seen.add(sym.hash)
                out.append(sym)
        except Exception:
            # Defensive — never crash the flow on a buggy detector.
            continue
    return out
