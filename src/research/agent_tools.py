"""Shared tool palette for the Operator + specialist agents (Phase 10).

Each agent imports this module and selects the subset of tools it is
allowed to use. The Operator gets delegations + reads only. The
specialists get their own action verbs. Code-enforced safety lives in
the underlying helpers (atomic registry, deterministic broker seam).

Pragmatic note: most tool implementations were authored under
``src.research.pm_agent`` and are reused unchanged. ``make_tools``
selects from the union and adds the new Phase 10 tools (agent_tasks
queue, YouTube search, language-fallback transcript fetcher).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.research import pm_agent as _pm

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# YouTube transcript with language fallback (replaces pm_agent's simple fetch)
# ---------------------------------------------------------------------------

_YT_ID_RX = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")
_YT_DEFAULT_CAP = 32_000


def _resolve_video_id(s: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _YT_ID_RX.search(s)
    return m.group(1) if m else None


def fetch_youtube_transcript(*, url_or_id: str, max_chars: int = _YT_DEFAULT_CAP) -> dict:
    """Fetch a YouTube transcript with English-first language fallback.

    Order: manual EN > generated EN > any generated > any manual.
    Handles ``TranscriptsDisabled``, ``NoTranscriptFound``, and
    ``VideoUnavailable`` gracefully.
    """
    if not isinstance(url_or_id, str) or not url_or_id.strip():
        return {"ok": False, "error": "url_or_id required"}
    s = url_or_id.strip()
    try:
        cap = int(max_chars)
    except (TypeError, ValueError):
        cap = _YT_DEFAULT_CAP
    cap = max(1024, min(cap, 500_000))
    video_id = _resolve_video_id(s)
    if not video_id:
        return {"ok": False, "error": "could not parse youtube video id"}
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import-not-found]
    except ImportError:
        return {"ok": False, "error": "youtube-transcript-api not installed"}
    try:
        from youtube_transcript_api import (  # type: ignore[import-not-found]
            TranscriptsDisabled, NoTranscriptFound,
        )
    except ImportError:  # older API
        TranscriptsDisabled = Exception  # type: ignore[misc,assignment]
        NoTranscriptFound = Exception  # type: ignore[misc,assignment]

    fetched = None
    used_lang = None
    try:
        # New (>=1.0) instance API.
        api = YouTubeTranscriptApi()
        if hasattr(api, "list"):
            try:
                listing = api.list(video_id)
                # Try EN manual first.
                for finder, label in (
                    (lambda lst: lst.find_manually_created_transcript(["en"]), "manual:en"),
                    (lambda lst: lst.find_generated_transcript(["en"]), "generated:en"),
                ):
                    try:
                        t = finder(listing)
                        fetched = t.fetch()
                        used_lang = label
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if fetched is None:
                    # Iterate listing for any usable transcript.
                    for t in listing:
                        try:
                            fetched = t.fetch()
                            used_lang = getattr(t, "language_code", "?")
                            break
                        except Exception:  # noqa: BLE001
                            continue
            except (TranscriptsDisabled, NoTranscriptFound) as exc:
                return {"ok": False, "error": f"no transcripts: {type(exc).__name__}"}
        if fetched is None:
            try:
                fetched = api.fetch(video_id)
            except AttributeError:
                fetched = YouTubeTranscriptApi.get_transcript(video_id)  # type: ignore[attr-defined]
    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        return {"ok": False, "error": f"no transcripts: {type(exc).__name__}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"youtube fetch failed: {exc}"}

    chunks: list[str] = []
    try:
        for snip in fetched:
            if hasattr(snip, "text"):
                chunks.append(str(snip.text))
            elif isinstance(snip, dict):
                chunks.append(str(snip.get("text", "")))
            else:
                chunks.append(str(snip))
    except TypeError:
        return {"ok": False, "error": "transcript not iterable"}
    transcript = " ".join(c for c in chunks if c).strip()
    truncated = False
    if len(transcript) > cap:
        transcript = transcript[:cap]
        truncated = True
    return {
        "ok": True,
        "video_id": video_id,
        "title": "",
        "language": used_lang or "?",
        "transcript": transcript,
        "truncated": truncated,
    }


def search_youtube_trading_videos(*, query: str, max_results: int = 5) -> dict:
    """Search YouTube via DuckDuckGo videos endpoint."""
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "query required"}
    try:
        n = int(max_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 25))
    try:
        try:
            from ddgs import DDGS  # type: ignore[import-not-found]
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ddg import failed: {exc}"}
    try:
        with DDGS() as ddg:
            try:
                raw = list(ddg.videos(query, max_results=n * 3))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"ddg videos failed: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"ddg session failed: {exc}"}
    out: list[dict] = []
    for r in raw:
        url = str(r.get("content") or r.get("url") or "")
        if "youtube.com" not in url and "youtu.be" not in url:
            continue
        vid = _resolve_video_id(url) or ""
        out.append({
            "title": str(r.get("title") or "")[:200],
            "url": url,
            "video_id": vid,
            "duration": str(r.get("duration") or ""),
            "channel": str(r.get("uploader") or r.get("channel") or ""),
            "snippet": str(r.get("description") or "")[:280],
        })
        if len(out) >= n:
            break
    return {"ok": True, "results": out}


# ---------------------------------------------------------------------------
# agent_tasks queue tools
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


_VALID_TEAMS = {"researcher", "trader", "registrar", "coder"}
_VALID_URGENCY = {"low", "normal", "high"}


def make_enqueue_task(*, db_path: str, requester: str = "operator") -> Callable[..., dict]:
    def enqueue_task(*, team: str, action: str, payload: dict, rationale: str,
                     urgency: str = "normal", parent_task_id: int | None = None) -> dict:
        if team not in _VALID_TEAMS:
            return {"ok": False, "error": f"unknown team: {team}"}
        if not isinstance(action, str) or not action.strip():
            return {"ok": False, "error": "action required"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}
        if urgency not in _VALID_URGENCY:
            urgency = "normal"
        from src.db.models import init_db
        init_db(db_path)
        with _connect(db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO agent_tasks
                  (requested_at_utc, requester, team, action, payload_json,
                   rationale, urgency, status, parent_task_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
                """,
                (_utc_now(), requester, team, action,
                 json.dumps(payload or {}, default=str),
                 rationale, urgency, parent_task_id),
            )
            tid = int(cur.lastrowid)
            conn.commit()
        return {"ok": True, "task_id": tid}
    return enqueue_task


def make_get_open_tasks(*, db_path: str, team: str) -> Callable[..., list[dict]]:
    def get_open_tasks(*, limit: int = 10) -> list[dict]:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, 100))
        from src.db.models import init_db
        init_db(db_path)
        urgency_order = "CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END"
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, requested_at_utc, requester, team, action,
                       payload_json, rationale, urgency, status, parent_task_id
                FROM agent_tasks
                WHERE team = ? AND status = 'open'
                ORDER BY {urgency_order}, requested_at_utc ASC
                LIMIT ?
                """,
                (team, n),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            out.append({
                "id": int(r["id"]),
                "requested_at_utc": str(r["requested_at_utc"]),
                "requester": str(r["requester"]),
                "team": str(r["team"]),
                "action": str(r["action"]),
                "payload": payload,
                "rationale": str(r["rationale"]),
                "urgency": str(r["urgency"]),
                "status": str(r["status"]),
                "parent_task_id": r["parent_task_id"],
            })
        return out
    return get_open_tasks


def make_claim_task(*, db_path: str, team: str, claimed_by: str) -> Callable[..., dict]:
    def claim_task(*, task_id: int) -> dict:
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "task_id must be int"}
        from src.db.models import init_db
        init_db(db_path)
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id, team, status FROM agent_tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {"ok": False, "error": f"task {tid} not found"}
            if str(row["team"]) != team:
                conn.rollback()
                return {"ok": False, "error": f"task {tid} is for team {row['team']!r}, not {team!r}"}
            if str(row["status"]) != "open":
                conn.rollback()
                return {"ok": False, "error": f"task {tid} status is {row['status']!r}, expected 'open'"}
            conn.execute(
                "UPDATE agent_tasks SET status='claimed', claimed_at_utc=?, claimed_by=? WHERE id=?",
                (_utc_now(), claimed_by, tid),
            )
            full = conn.execute(
                "SELECT id, requested_at_utc, requester, team, action, payload_json, "
                "rationale, urgency, status, parent_task_id FROM agent_tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()
        try:
            payload = json.loads(full["payload_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return {
            "ok": True,
            "task": {
                "id": int(full["id"]),
                "team": str(full["team"]),
                "action": str(full["action"]),
                "payload": payload,
                "rationale": str(full["rationale"]),
                "urgency": str(full["urgency"]),
                "requester": str(full["requester"]),
            },
        }
    return claim_task


def make_complete_task(*, db_path: str) -> Callable[..., dict]:
    def complete_task(*, task_id: int, result: dict, status: str = "done") -> dict:
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "task_id must be int"}
        if status not in ("done", "failed", "abandoned"):
            return {"ok": False, "error": f"invalid status: {status}"}
        from src.db.models import init_db
        init_db(db_path)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT status FROM agent_tasks WHERE id = ?", (tid,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"task {tid} not found"}
            conn.execute(
                "UPDATE agent_tasks SET status=?, completed_at_utc=?, result_json=? WHERE id=?",
                (status, _utc_now(), json.dumps(result or {}, default=str), tid),
            )
            conn.commit()
        return {"ok": True, "task_id": tid, "status": status}
    return complete_task


# ---------------------------------------------------------------------------
# Trader-only: cancel order, get fills
# ---------------------------------------------------------------------------


def cancel_order(*, order_id: int) -> dict:
    """Best-effort cancel via the deterministic broker seam.

    The broker seam reuses the IBKR connection used by ``place_bracket``;
    on failure (no IBKR / dry-run / unknown id) returns
    ``{ok: False}`` so the agent can mark the task failed without
    crashing the loop.
    """
    try:
        oid = int(order_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "order_id must be int"}
    try:
        from src.portfolio_runtime import cancel_order as _impl
        result = _impl(order_id=oid)
        return {"ok": True, "order_id": oid, "status": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def make_get_fills(*, db_path: str) -> Callable[..., list[dict]]:
    def get_fills(*, limit: int = 20) -> list[dict]:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 20
        n = max(1, min(n, 200))
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, signal_id, strategy_id, symbol, direction, qty,
                       entry_fill, exit_fill, pnl_dollars, status,
                       opened_at, closed_at
                FROM trades
                ORDER BY id DESC LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]
    return get_fills


# ---------------------------------------------------------------------------
# Registrar-only: reactivate_demoted
# ---------------------------------------------------------------------------


def reactivate_demoted(*, demotion_id: int) -> dict:
    try:
        did = int(demotion_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "demotion_id must be int"}
    try:
        from src import strategy_registry as sr
        new_id = sr.reactivate_demoted(demotion_id=did)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "new_strategy_id": int(new_id)}


# ---------------------------------------------------------------------------
# Findings — shared persistent memory across all agents
# ---------------------------------------------------------------------------


_VALID_FINDING_STATUSES = {"active", "superseded", "retracted"}


def make_record_finding(*, db_path: str, author: str) -> Callable[..., dict]:
    """Record a finding into the shared memory log.

    The ``author`` is bound at factory time (one of operator/researcher/
    trader/registrar/coder/human). On supersedes, the prior row's
    status is flipped to 'superseded' atomically with the insert.
    """
    def record_finding(*, title: str, body: str,
                       strategy_id: int | None = None,
                       strategy_name: str | None = None,
                       symbol: str | None = None,
                       refs: dict | None = None,
                       supersedes_id: int | None = None) -> dict:
        if not isinstance(title, str) or not title.strip():
            return {"ok": False, "error": "title required"}
        if not isinstance(body, str) or not body.strip():
            return {"ok": False, "error": "body required"}
        sid = int(strategy_id) if strategy_id is not None else None
        sup = int(supersedes_id) if supersedes_id is not None else None
        refs_json = json.dumps(refs or {}, default=str) if refs else None
        from src.db.models import init_db
        init_db(db_path)
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            superseded_id: int | None = None
            if sup is not None:
                row = conn.execute(
                    "SELECT id FROM findings WHERE id = ?", (sup,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return {"ok": False, "error": f"supersedes_id {sup} not found"}
                conn.execute(
                    "UPDATE findings SET status='superseded' WHERE id = ?",
                    (sup,),
                )
                superseded_id = sup
            cur = conn.execute(
                """
                INSERT INTO findings
                  (created_at_utc, author, title, body, strategy_id,
                   strategy_name, symbol, refs_json, supersedes_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (_utc_now(), author, title.strip(), body.strip(),
                 sid, strategy_name, symbol, refs_json, sup),
            )
            fid = int(cur.lastrowid)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()
        out: dict = {"ok": True, "finding_id": fid}
        if superseded_id is not None:
            out["superseded_id"] = superseded_id
        return out
    return record_finding


def make_read_findings(*, db_path: str) -> Callable[..., list[dict]]:
    def read_findings(*, query: str | None = None,
                      strategy_id: int | None = None,
                      strategy_name: str | None = None,
                      symbol: str | None = None,
                      author: str | None = None,
                      since_days: int = 30,
                      include_status: list[str] | None = None,
                      limit: int = 20) -> list[dict]:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = 20
        n = max(1, min(n, 100))
        try:
            days = int(since_days)
        except (TypeError, ValueError):
            days = 30
        days = max(1, days)
        statuses = list(include_status) if include_status else ["active"]
        statuses = [s for s in statuses if s in _VALID_FINDING_STATUSES] or ["active"]
        from src.db.models import init_db
        init_db(db_path)
        clauses: list[str] = ["created_at_utc >= datetime('now', ?)"]
        args: list[Any] = [f"-{days} days"]
        if query:
            clauses.append("(title LIKE ? OR body LIKE ?)")
            like = f"%{query}%"
            args.extend([like, like])
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            args.append(int(strategy_id))
        if strategy_name:
            clauses.append("strategy_name = ?")
            args.append(strategy_name)
        if symbol:
            clauses.append("symbol = ?")
            args.append(symbol)
        if author:
            clauses.append("author = ?")
            args.append(author)
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        args.extend(statuses)
        where = " AND ".join(clauses)
        args.append(n)
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT id, created_at_utc, author, title, body, strategy_id,
                       strategy_name, symbol, refs_json, supersedes_id, status
                FROM findings
                WHERE {where}
                ORDER BY created_at_utc DESC, id DESC
                LIMIT ?
                """,
                tuple(args),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                refs = json.loads(r["refs_json"]) if r["refs_json"] else {}
            except (TypeError, json.JSONDecodeError):
                refs = {}
            out.append({
                "id": int(r["id"]),
                "created_at_utc": str(r["created_at_utc"]),
                "author": str(r["author"]),
                "title": str(r["title"]),
                "body": str(r["body"]),
                "strategy_id": r["strategy_id"],
                "strategy_name": r["strategy_name"],
                "symbol": r["symbol"],
                "refs": refs,
                "supersedes_id": r["supersedes_id"],
                "status": str(r["status"]),
            })
        return out
    return read_findings


def make_retract_finding(*, db_path: str, author: str) -> Callable[..., dict]:
    def retract_finding(*, id: int, reason: str) -> dict:
        try:
            fid = int(id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be int"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason required"}
        from src.db.models import init_db
        init_db(db_path)
        suffix = f" [retracted by {author}: {reason.strip()}]"
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, body FROM findings WHERE id = ?", (fid,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"finding {fid} not found"}
            new_body = str(row["body"]) + suffix
            conn.execute(
                "UPDATE findings SET status='retracted', body=? WHERE id=?",
                (new_body, fid),
            )
            conn.commit()
        return {"ok": True, "id": fid}
    return retract_finding


def make_get_strategy_dossier(*, db_path: str) -> Callable[..., dict]:
    def get_strategy_dossier(*, strategy_id: int | None = None,
                             strategy_name: str | None = None) -> dict:
        if strategy_id is None and not strategy_name:
            return {"ok": False, "error": "strategy_id or strategy_name required"}
        from src.db.models import init_db
        init_db(db_path)
        with _connect(db_path) as conn:
            active = None
            family: str | None = None
            if strategy_id is not None:
                arow = conn.execute(
                    "SELECT * FROM active_strategies WHERE id = ?",
                    (int(strategy_id),),
                ).fetchone()
                if arow is not None:
                    active = dict(arow)
                    family = str(arow["strategy_family"])
            elif strategy_name:
                arow = conn.execute(
                    "SELECT * FROM active_strategies WHERE strategy_family = ? "
                    "AND state = 'active' ORDER BY id DESC LIMIT 1",
                    (strategy_name,),
                ).fetchone()
                if arow is not None:
                    active = dict(arow)
                family = strategy_name

            # Findings: by id OR by name (whichever provided; OR them).
            f_clauses: list[str] = []
            f_args: list[Any] = []
            if strategy_id is not None:
                f_clauses.append("strategy_id = ?")
                f_args.append(int(strategy_id))
            if family:
                f_clauses.append("strategy_name = ?")
                f_args.append(family)
            if not f_clauses:
                findings_rows = []
            else:
                where_f = " OR ".join(f_clauses)
                findings_rows = conn.execute(
                    f"""
                    SELECT id, created_at_utc, author, title, body, strategy_id,
                           strategy_name, symbol, refs_json, supersedes_id, status
                    FROM findings WHERE ({where_f}) AND status = 'active'
                    ORDER BY created_at_utc DESC, id DESC LIMIT 20
                    """,
                    tuple(f_args),
                ).fetchall()
            findings: list[dict] = []
            for r in findings_rows:
                try:
                    refs = json.loads(r["refs_json"]) if r["refs_json"] else {}
                except (TypeError, json.JSONDecodeError):
                    refs = {}
                findings.append({
                    "id": int(r["id"]),
                    "created_at_utc": str(r["created_at_utc"]),
                    "author": str(r["author"]),
                    "title": str(r["title"]),
                    "body": str(r["body"]),
                    "strategy_id": r["strategy_id"],
                    "strategy_name": r["strategy_name"],
                    "symbol": r["symbol"],
                    "refs": refs,
                    "supersedes_id": r["supersedes_id"],
                    "status": str(r["status"]),
                })

            demotions: list[dict] = []
            if strategy_id is not None:
                drows = conn.execute(
                    "SELECT * FROM auto_demotions WHERE strategy_id = ? "
                    "ORDER BY ts_utc DESC LIMIT 5",
                    (int(strategy_id),),
                ).fetchall()
                demotions = [dict(r) for r in drows]
            elif family:
                drows = conn.execute(
                    "SELECT * FROM auto_demotions WHERE strategy_family = ? "
                    "ORDER BY ts_utc DESC LIMIT 5",
                    (family,),
                ).fetchall()
                demotions = [dict(r) for r in drows]

            candidates: list[dict] = []
            if family:
                crows = conn.execute(
                    "SELECT * FROM strategy_candidates WHERE strategy_family = ? "
                    "ORDER BY ts_utc DESC LIMIT 5",
                    (family,),
                ).fetchall()
                candidates = [dict(r) for r in crows]

            recent_trades: list[dict] = []
            recent_signals: list[dict] = []
            if strategy_id is not None:
                trows = conn.execute(
                    "SELECT * FROM trades WHERE strategy_id = ? "
                    "ORDER BY id DESC LIMIT 10",
                    (int(strategy_id),),
                ).fetchall()
                recent_trades = [dict(r) for r in trows]
                srows = conn.execute(
                    "SELECT * FROM signals WHERE strategy_id = ? "
                    "ORDER BY id DESC LIMIT 10",
                    (int(strategy_id),),
                ).fetchall()
                recent_signals = [dict(r) for r in srows]
            elif family:
                trows = conn.execute(
                    "SELECT * FROM trades WHERE strategy_family = ? "
                    "ORDER BY id DESC LIMIT 10",
                    (family,),
                ).fetchall()
                recent_trades = [dict(r) for r in trows]
                srows = conn.execute(
                    "SELECT * FROM signals WHERE strategy_family = ? "
                    "ORDER BY id DESC LIMIT 10",
                    (family,),
                ).fetchall()
                recent_signals = [dict(r) for r in srows]

        return {
            "ok": True,
            "active": active,
            "findings": findings,
            "demotions": demotions,
            "candidates": candidates,
            "recent_trades": recent_trades,
            "recent_signals": recent_signals,
        }
    return get_strategy_dossier


# ---------------------------------------------------------------------------
# Tool palette assembly
# ---------------------------------------------------------------------------


# Every tool has a (callable, schema) entry so agents pick what they need.
# Implementations come from pm_agent's _make_tools where possible (DRY) and
# from this module for new Phase 10 surfaces.

# Tools that, when invoked, populate decision_result.actions_taken.
_ACTION_TOOL_NAMES = {
    "retire_strategy", "promote_candidate", "modify_strategy_params",
    "place_paper_order", "place_bracket_order", "cancel_order",
    "propose_candidate", "propose_custom_strategy", "retire_custom_strategy",
    "run_judas_threshold_sweep", "run_walk_forward", "run_custom_backtest",
    "reactivate_demoted",
    # delegation tools — count as actions for the operator
    "delegate_to_researcher", "delegate_to_trader",
    "delegate_to_registrar", "delegate_to_coder",
    # queue mutators
    "claim_task", "complete_task",
}


def _safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(**kwargs) -> Any:
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("agent_tools.tool.failed", extra={"tool": fn.__name__})
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    wrapped.__name__ = fn.__name__
    return wrapped


def _all_pm_tools(db_path: str) -> dict[str, Callable[..., Any]]:
    """Return pm_agent's existing tool palette."""
    return _pm._make_tools(db_path=db_path)


def _all_pm_schemas() -> list[dict]:
    return _pm._tool_schemas()


def _new_schemas() -> list[dict]:
    """Schemas for Phase 10-only tools."""
    sym_enum = sorted(_pm._VALID_SYMBOLS)
    return [
        {
            "type": "function",
            "function": {
                "name": "search_youtube_trading_videos",
                "description": "DuckDuckGo videos search filtered to YouTube; returns title/url/video_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "place_bracket_order",
                "description": (
                    "Place a paper bracket via the deterministic broker. "
                    "Inserts a signals row first; returns ibkr_order_ids on success. "
                    "Holds the IBKR connection until the bracket confirms."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "side": {"type": "string", "enum": ["BUY", "SELL"]},
                        "quantity": {"type": "integer"},
                        "stop_price": {"type": "number"},
                        "target_price": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "symbol", "side", "quantity",
                        "stop_price", "target_price", "rationale",
                    ],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "cancel_order",
                "description": "Cancel a paper IBKR order by id.",
                "parameters": {
                    "type": "object",
                    "properties": {"order_id": {"type": "integer"}},
                    "required": ["order_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_fills",
                "description": "Recent trades with fill info, P&L, and status.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reactivate_demoted",
                "description": "Re-insert a previously demoted strategy from auto_demotions.",
                "parameters": {
                    "type": "object",
                    "properties": {"demotion_id": {"type": "integer"}},
                    "required": ["demotion_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "claim_task",
                "description": "Claim an open agent_tasks row for this team.",
                "parameters": {
                    "type": "object",
                    "properties": {"task_id": {"type": "integer"}},
                    "required": ["task_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "complete_task",
                "description": "Mark a claimed agent_tasks row done/failed/abandoned with result_json.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                        "result": {"type": "object"},
                        "status": {"type": "string", "enum": ["done", "failed", "abandoned"]},
                    },
                    "required": ["task_id", "result"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_open_tasks",
                "description": "List open agent_tasks rows assigned to this team.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 10}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_to_researcher",
                "description": "Queue a research task for the Researcher specialist.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                        "rationale": {"type": "string"},
                    },
                    "required": ["topic", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_to_trader",
                "description": "Queue a trade for the Trader specialist.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "side": {"type": "string", "enum": ["BUY", "SELL"]},
                        "qty": {"type": "integer"},
                        "stop": {"type": "number"},
                        "target": {"type": "number"},
                        "rationale": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                    },
                    "required": ["symbol", "side", "qty", "stop", "target", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_to_registrar",
                "description": "Queue a registry mutation (retire/promote/modify/reactivate) for the Registrar.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": [
                            "retire_strategy", "promote_candidate",
                            "modify_strategy_params", "reactivate_demoted",
                        ]},
                        "target_id": {"type": "integer"},
                        "params": {"type": "object"},
                        "reason": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                    },
                    "required": ["action", "target_id", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delegate_to_coder",
                "description": "Trigger Phase 3 autofix for a symptom.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symptom": {"type": "string"},
                        "context": {"type": "string"},
                        "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
                    },
                    "required": ["symptom", "context"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "record_finding",
                "description": (
                    "Record a finding into the team's shared persistent memory. "
                    "Use it whenever you discover something worth remembering "
                    "across cycles (a strategy weakness, a regime observation, "
                    "a recurring bug pattern, etc.)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "strategy_id": {"type": "integer"},
                        "strategy_name": {"type": "string"},
                        "symbol": {"type": "string"},
                        "refs": {"type": "object"},
                        "supersedes_id": {"type": "integer"},
                    },
                    "required": ["title", "body"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_findings",
                "description": (
                    "Read recent findings from the team's shared memory. All "
                    "filters optional and ANDed; default returns last 30 days "
                    "of active findings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "strategy_id": {"type": "integer"},
                        "strategy_name": {"type": "string"},
                        "symbol": {"type": "string"},
                        "author": {"type": "string"},
                        "since_days": {"type": "integer", "default": 30},
                        "include_status": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": ["active"],
                        },
                        "limit": {"type": "integer", "default": 20},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retract_finding",
                "description": (
                    "Mark a finding as retracted with a reason. The reason is "
                    "appended to the body."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_strategy_dossier",
                "description": (
                    "One-shot snapshot of everything the team knows about a "
                    "strategy: active row, recent findings, demotions, "
                    "candidates with the same name, recent trades, recent "
                    "signals. Pass strategy_id OR strategy_name."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "strategy_id": {"type": "integer"},
                        "strategy_name": {"type": "string"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_outstanding_delegations",
                "description": "Recent agent_tasks rows the Operator has issued (any team).",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}},
                },
            },
        },
    ]


def make_tools(*, db_path: str, include: set[str] | None = None,
               team: str | None = None,
               claimed_by: str | None = None,
               author: str | None = None,
               operator_mode: bool = False) -> tuple[dict[str, Callable[..., Any]], list[dict]]:
    """Return ``(tools_dict, schemas_list)`` filtered to ``include``.

    - ``team`` controls which team's queue claim/complete/get_open_tasks bind to.
    - ``operator_mode=True`` synthesizes the four ``delegate_to_*`` tools that
      enqueue rows on the operator's behalf.
    """
    pm_tools = _all_pm_tools(db_path)
    pm_schemas = _all_pm_schemas()

    # Add Phase 10 tools.
    extras: dict[str, Callable[..., Any]] = {
        "search_youtube_trading_videos": _safe_tool(search_youtube_trading_videos),
        # Override pm_agent's basic transcript with the language-fallback version.
        "fetch_youtube_transcript": _safe_tool(fetch_youtube_transcript),
        "place_bracket_order": pm_tools["place_paper_order"],  # alias
        "cancel_order": _safe_tool(cancel_order),
        "get_fills": _safe_tool(make_get_fills(db_path=db_path)),
        "reactivate_demoted": _safe_tool(reactivate_demoted),
    }
    if team is not None:
        cb = claimed_by or f"{team}_agent"
        extras["claim_task"] = _safe_tool(make_claim_task(db_path=db_path, team=team, claimed_by=cb))
        extras["complete_task"] = _safe_tool(make_complete_task(db_path=db_path))
        extras["get_open_tasks"] = _safe_tool(make_get_open_tasks(db_path=db_path, team=team))

    # Findings — shared across all agents. Author defaults to team if omitted,
    # else 'operator' when operator_mode, else 'human' as a sensible fallback.
    effective_author = author or team or ("operator" if operator_mode else "human")
    extras["record_finding"] = _safe_tool(
        make_record_finding(db_path=db_path, author=effective_author))
    extras["read_findings"] = _safe_tool(make_read_findings(db_path=db_path))
    extras["retract_finding"] = _safe_tool(
        make_retract_finding(db_path=db_path, author=effective_author))
    extras["get_strategy_dossier"] = _safe_tool(make_get_strategy_dossier(db_path=db_path))

    if operator_mode:
        enq = make_enqueue_task(db_path=db_path, requester="operator")

        def delegate_to_researcher(*, topic: str, rationale: str,
                                   urgency: str = "normal") -> dict:
            return enq(team="researcher", action="research_topic",
                       payload={"topic": topic}, rationale=rationale, urgency=urgency)

        def delegate_to_trader(*, symbol: str, side: str, qty: int,
                               stop: float, target: float, rationale: str,
                               urgency: str = "normal") -> dict:
            return enq(team="trader", action="place_trade",
                       payload={"symbol": symbol.upper(), "side": side.upper(),
                                "qty": int(qty), "stop": float(stop),
                                "target": float(target)},
                       rationale=rationale, urgency=urgency)

        def delegate_to_registrar(*, action: str, target_id: int,
                                  reason: str, params: dict | None = None,
                                  urgency: str = "normal") -> dict:
            return enq(team="registrar", action=action,
                       payload={"target_id": int(target_id),
                                "params": params or {}},
                       rationale=reason, urgency=urgency)

        def delegate_to_coder(*, symptom: str, context: str,
                              urgency: str = "normal") -> dict:
            return enq(team="coder", action="autofix_symptom",
                       payload={"symptom": symptom, "context": context},
                       rationale=symptom, urgency=urgency)

        def get_outstanding_delegations(*, limit: int = 20) -> list[dict]:
            with _connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, requested_at_utc, team, action, urgency, status,
                           rationale, payload_json
                    FROM agent_tasks
                    WHERE requester = 'operator'
                    ORDER BY id DESC LIMIT ?
                    """,
                    (max(1, min(int(limit), 100)),),
                ).fetchall()
            out: list[dict] = []
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    payload = {}
                out.append({
                    "id": int(r["id"]),
                    "requested_at_utc": str(r["requested_at_utc"]),
                    "team": str(r["team"]),
                    "action": str(r["action"]),
                    "urgency": str(r["urgency"]),
                    "status": str(r["status"]),
                    "rationale": str(r["rationale"]),
                    "payload": payload,
                })
            return out

        extras["delegate_to_researcher"] = _safe_tool(delegate_to_researcher)
        extras["delegate_to_trader"] = _safe_tool(delegate_to_trader)
        extras["delegate_to_registrar"] = _safe_tool(delegate_to_registrar)
        extras["delegate_to_coder"] = _safe_tool(delegate_to_coder)
        extras["get_outstanding_delegations"] = _safe_tool(get_outstanding_delegations)

    # Schema for get_recent_trades alias used by Operator
    def get_recent_trades(*, limit: int = 20) -> list[dict]:
        # Reuse get_fills shape.
        return make_get_fills(db_path=db_path)(limit=limit)
    extras["get_recent_trades"] = _safe_tool(get_recent_trades)

    all_tools: dict[str, Callable[..., Any]] = dict(pm_tools)
    all_tools.update(extras)

    extra_schemas = _new_schemas()
    # Schema for get_recent_trades:
    extra_schemas.append({
        "type": "function",
        "function": {
            "name": "get_recent_trades",
            "description": "Recent trades (alias for get_fills).",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        },
    })
    all_schemas: dict[str, dict] = {s["function"]["name"]: s for s in (pm_schemas + extra_schemas)}

    if include is None:
        return all_tools, list(all_schemas.values())

    selected_tools = {k: v for k, v in all_tools.items() if k in include}
    selected_schemas = [all_schemas[name] for name in include if name in all_schemas]
    return selected_tools, selected_schemas


def is_action_tool(name: str) -> bool:
    return name in _ACTION_TOOL_NAMES
