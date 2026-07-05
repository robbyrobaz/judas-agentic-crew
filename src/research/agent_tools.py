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
import subprocess
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


def make_flatten_position(*, db_path: str) -> Callable[..., dict]:
    """Build the ``flatten_position`` trader tool with ``db_path`` bound.

    This was previously defined as a bare function with a hardcoded
    ``_sql.connect(db_path)`` call, but ``db_path`` was never injected
    into the scope. The bug surfaced as a ``NameError: name 'db_path' is
    not defined`` whenever the trader called flatten_position — blocking
    the sleeve-check pre-trade step (finding 922ef65e). The factory
    pattern here matches every other db-aware tool in this module
    (``make_get_fills``, ``make_claim_task``, etc.).
    """
    def flatten_position(*, symbol: str, side: str, qty: int) -> dict:
        """Close an existing futures position with a single MARKET order — no
        bracket children. This is the clean way to flatten a naked position
        without orphaning stop/target orders that would re-open it.

        Args:
          symbol: 'MGC', 'MNQ', 'MCL', 'MBT', 'MET', 'DX', 'ZF', '6J'
          side:   'close_long'  → places SELL  (closes a long)
                  'close_short' → places BUY   (closes a short)
          qty: positive integer, contracts to close.

        Returns: {ok, action, qty, symbol, parent_order_id, status, error}.
        """
        sym = str(symbol).upper()
        if sym not in _pm._VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        s = str(side).lower().strip()
        if s in ("close_long", "sell", "long"):
            action = "SELL"
        elif s in ("close_short", "buy", "short"):
            action = "BUY"
        else:
            return {"ok": False, "error": "side must be close_long or close_short"}
        try:
            n = int(qty)
        except (TypeError, ValueError):
            return {"ok": False, "error": "qty must be int"}
        if n <= 0:
            return {"ok": False, "error": "qty must be > 0"}

        # Sleeve guard: refuse to flatten any symbol we don't have an open
        # trades row for. The IBKR paper account is shared with the workshop
        # and options-recorder — we must not close their positions.
        try:
            conn = _connect(db_path)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE status='open' "
                    "AND symbol = ? AND direction = ?",
                    (sym, "long" if action == "SELL" else "short"),
                ).fetchone()
                sleeve_open = int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"sleeve check failed: {exc}"}
        if sleeve_open == 0:
            return {"ok": False,
                    "error": f"no open agentic-crew trade for {sym}/{action} — "
                             "refusing to flatten (position may belong to another "
                             "tenant on this paper account)"}

        # Confirm position exists on IBKR before placing the close order.
        try:
            from ib_async import IB, Future, MarketOrder
            ib = IB()
            ib.connect("127.0.0.1", 4002, clientId=199, timeout=8)
            try:
                current = None
                for p in ib.positions():
                    if (p.contract.secType == "FUT"
                            and p.contract.symbol == sym
                            and p.position != 0):
                        current = p
                        break
                if current is None:
                    return {"ok": False, "error": f"no open {sym} position to flatten"}
                # Sanity: side must match the actual position
                held_dir = "long" if current.position > 0 else "short"
                requested = "close_short" if action == "BUY" else "close_long"
                if (requested == "close_long" and held_dir != "long") or \
                   (requested == "close_short" and held_dir != "short"):
                    return {
                        "ok": False,
                        "error": f"side mismatch: requested {requested} but position is {held_dir}",
                    }
                held_qty = abs(int(current.position))
                close_qty = min(n, held_qty)
                # Re-qualify the front-month contract
                cont = Future(current.contract.symbol,
                              lastTradeDateOrContractMonth=current.contract.lastTradeDateOrContractMonth,
                              exchange=current.contract.exchange)
                q = ib.qualifyContracts(cont)
                if not q:
                    return {"ok": False, "error": "could not qualify contract"}
                order = MarketOrder(action, close_qty)
                order.tif = "GTC"
                order.outsideRth = True
                trade = ib.placeOrder(q[0], order)
                # Brief wait for status to flip from PendingSubmit
                for _ in range(10):
                    ib.sleep(0.3)
                    st = trade.orderStatus.status
                    if st in ("Submitted", "PreSubmitted", "Filled"):
                        break
                return {
                    "ok": True,
                    "action": action,
                    "symbol": sym,
                    "qty": close_qty,
                    "parent_order_id": order.orderId,
                    "status": trade.orderStatus.status or "Submitted",
                    "local_symbol": q[0].localSymbol,
                }
            finally:
                ib.disconnect()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return flatten_position


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


def insert_custom_strategy(
    *, symbol: str, strategy_family: str, strategy_name: str,
    params_json: str, source_candidate_id: int | None = None,
) -> dict:
    """Insert a new strategy into active_strategies. Use this when promoting a candidate to active paper trading."""
    try:
        from src import strategy_registry as sr
        new_id = sr.insert_active_strategy(
            symbol=symbol,
            strategy_family=strategy_family,
            params_json=params_json,
            source_candidate_id=source_candidate_id,
        )
        return {"ok": True, "strategy_id": int(new_id)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Findings — shared persistent memory across all agents
# ---------------------------------------------------------------------------


_VALID_FINDING_STATUSES = {"active", "superseded", "retracted"}


def make_record_finding(*, db_path: str, author: str) -> Callable[..., dict]:
    """Record a finding into CrewAI Memory (vector + composite scoring).

    Same tool signature as before — prompts didn't change. Importance
    is inferred from author + content (human → 0.98, cycle-status pings
    → 0.1, default → 0.6) so registrar 'queue empty' noise stops
    drowning out human guidance and researcher discoveries.

    `supersedes_id` is accepted for back-compat but is now a soft
    pointer in metadata; CrewAI's consolidation flow handles dedup/
    update on similar content automatically.
    """
    from src.research import memory_backend

    def record_finding(*, title: str, body: str,
                       strategy_id: int | None = None,
                       strategy_name: str | None = None,
                       symbol: str | None = None,
                       refs: dict | None = None,
                       supersedes_id: int | None = None) -> dict:
        merged_refs = dict(refs or {})
        if supersedes_id is not None:
            try:
                merged_refs["supersedes_id"] = int(supersedes_id)
            except (TypeError, ValueError):
                pass
        result = memory_backend.save_memory(
            author=author, title=title, body=body,
            strategy_id=strategy_id, strategy_name=strategy_name,
            symbol=symbol, refs=merged_refs or None,
        )
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "finding_id": result.get("id"),
            "importance": result.get("importance"),
        }
    return record_finding


def make_read_findings(*, db_path: str) -> Callable[..., list[dict]]:
    """Search the team's persistent memory (CrewAI Memory backend).

    Same signature as before; backed by composite scoring (importance +
    semantic + recency) so human-authored guidance surfaces ahead of
    registrar cycle-status pings.

    `strategy_id`, `since_days`, and `include_status` are accepted for
    back-compat but no longer drive a SQL filter — the memory ranks by
    relevance, not date windows. `symbol`, `author`, `query`, and
    `limit` are honored.
    """
    from src.research import memory_backend

    def read_findings(*, query: str | None = None,
                      strategy_id: int | None = None,
                      strategy_name: str | None = None,
                      symbol: str | None = None,
                      author: str | None = None,
                      since_days: int = 30,
                      include_status: list[str] | None = None,
                      limit: int = 20) -> list[dict]:
        # If caller filters by strategy_id but not query, synthesize a
        # query so the recall can semantically narrow to that strategy.
        effective_query = query
        if not effective_query and strategy_name:
            effective_query = f"strategy {strategy_name}"
        elif not effective_query and strategy_id is not None:
            effective_query = f"strategy id {strategy_id}"
        return memory_backend.search_memory(
            query=effective_query, author=author, symbol=symbol, limit=limit,
        )
    return read_findings


def make_retract_finding(*, db_path: str, author: str) -> Callable[..., dict]:
    """Remove a memory by id from CrewAI Memory."""
    from src.research import memory_backend

    def retract_finding(*, id, reason: str) -> dict:
        # `id` may be a UUID string (memory backend) or int (legacy
        # findings table fallback). Accept either.
        record_id = str(id) if id is not None else ""
        if not record_id:
            return {"ok": False, "error": "id required"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason required"}
        result = memory_backend.forget_memory(record_id=record_id)
        if not result.get("ok"):
            return result
        return {"ok": True, "id": record_id, "removed": result.get("removed", 0)}
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

            # Findings now come from CrewAI Memory via the backend, not
            # the legacy SQL table. Use the strategy name/id as a query
            # so semantic recall surfaces related notes.
            from src.research import memory_backend
            mem_query: str | None = None
            if family and strategy_id is not None:
                mem_query = f"strategy {family} id {strategy_id}"
            elif family:
                mem_query = f"strategy {family}"
            elif strategy_id is not None:
                mem_query = f"strategy id {strategy_id}"
            findings: list[dict] = []
            if mem_query:
                findings = memory_backend.search_memory(
                    query=mem_query, limit=20,
                )

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
    "insert_active_strategy",
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


# ---------------------------------------------------------------------------
# Write/edit/shell tools — give the agents real hands (2026-06-29).
# The ONE limit (Rob): they must NOT edit files outside this repo or touch
# other repos. write_file/edit_file enforce that AIRTIGHT (resolve realpath,
# refuse any escape via .., symlinks, or absolute paths outside the repo).
# run_shell can't be kernel-jailed here (unprivileged userns is disabled, no
# root), so it's confined best-effort: cwd=repo + reject commands referencing
# out-of-repo /home paths or parent-traversal. Not a hard wall — a guard.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _confine_to_repo(path: str) -> Path:
    """Resolve `path` (relative to repo root if not absolute) and REFUSE if it
    escapes the repo — other repos, system files, via .. or symlinks. Raises
    ValueError on escape."""
    p = Path(path)
    if not p.is_absolute():
        p = _REPO_ROOT / p
    rp = p.resolve()
    if rp != _REPO_ROOT and _REPO_ROOT not in rp.parents:
        raise ValueError(
            f"refused: '{path}' resolves outside the repo ({_REPO_ROOT}). "
            f"Agents may only write within this repo."
        )
    return rp


def write_file(*, path: str, content: str) -> dict:
    """Write `content` to `path` (confined to this repo). Creates parent dirs."""
    try:
        rp = _confine_to_repo(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(rp), "bytes": len(content)}
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}


def edit_file(*, path: str, old: str, new: str) -> dict:
    """Replace the (unique) `old` substring with `new` in `path` (repo-confined)."""
    try:
        rp = _confine_to_repo(path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not rp.exists():
        return {"ok": False, "error": f"file not found: {rp}"}
    try:
        text = rp.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    n = text.count(old)
    if n == 0:
        return {"ok": False, "error": "old string not found"}
    if n > 1:
        return {"ok": False, "error": f"old string not unique ({n} matches) — add context"}
    try:
        rp.write_text(text.replace(old, new, 1), encoding="utf-8")
        return {"ok": True, "path": str(rp)}
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}


# Out-of-repo write vectors a shell command must not reference. /home/rob/<other>
# is the explicit concern (other repos). cwd is forced to the repo.
_SHELL_OUT_OF_REPO = re.compile(
    r"(^|[\s'\"=(])(/home/[^\s'\"]+|\.\.(/|\\)|/etc/|/usr/(?!bin|lib|share)|/root/|~/)",
)


def _shell_escape_reason(command: str) -> str | None:
    repo = str(_REPO_ROOT)
    for m in _SHELL_OUT_OF_REPO.finditer(command):
        tok = m.group(2)
        # Allow absolute references that stay inside the repo.
        if tok.startswith("/home/") and not tok.startswith(repo):
            return f"references an out-of-repo path: {tok}"
        if tok.startswith((".." "/", "..\\")) or tok == "..":
            return "uses parent-directory traversal (..)"
        if tok.startswith(("/etc/", "/root/", "~/")):
            return f"references a system/home path: {tok}"
    return None


def run_shell(*, command: str, timeout_s: int = 120) -> dict:
    """Run a shell command with cwd=repo. Best-effort confined to the repo
    (NOT a kernel jail — see module note). Rejects out-of-repo path references."""
    reason = _shell_escape_reason(command)
    if reason:
        return {"ok": False, "error": f"refused: command {reason}. "
                f"run_shell is confined to {_REPO_ROOT}."}
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=timeout_s,
        )
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-8000:],
                "stderr": (proc.stderr or "")[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout_s}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"run_shell failed: {exc}"}


def reap_stale_claims(*, db_path: str, max_age_hours: float = 24.0) -> int:
    """Abandon tasks stuck in 'claimed' longer than max_age_hours.

    An agent that claims a task then dies/times out before complete_task wedges
    the task in 'claimed' FOREVER — it never reappears in get_open_tasks, so the
    work is silently lost (38 zombies accumulated May-Jun 2026 this way). Mark
    them 'abandoned' (not 'open': re-opening ancient tasks would flood the queue
    with stale work; the operator re-delegates anything still relevant).
    Returns the number reaped. Never raises."""
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'abandoned',
                    result_json = json_object('reaped',
                        'claimed > ' || ? || 'h without completion')
                WHERE status = 'claimed'
                  AND claimed_at_utc < datetime('now', ?)
                """,
                (max_age_hours, f"-{max_age_hours} hours"),
            )
            n = cur.rowcount
            conn.commit()
        if n:
            log.warning("agent_tasks.reaped_stale_claims n=%d", n)
        return int(n)
    except Exception:  # noqa: BLE001
        log.exception("reap_stale_claims failed")
        return 0


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
        "flatten_position": _safe_tool(make_flatten_position(db_path=db_path)),
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
    # Real hands — repo-confined write/edit/shell (2026-06-29). Available to every
    # agent that includes them; the confinement lives in the tools themselves.
    extras["write_file"] = _safe_tool(write_file)
    extras["edit_file"] = _safe_tool(edit_file)
    extras["run_shell"] = _safe_tool(run_shell)
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
            # 1. Record the delegation on agent_tasks so the team-eye-view
            #    dashboard reflects what the Operator asked for.
            enq_result = enq(team="coder", action="autofix_symptom",
                             payload={"symptom": symptom, "context": context},
                             rationale=symptom, urgency=urgency)
            task_id = None
            if isinstance(enq_result, dict):
                task_id = enq_result.get("task_id") or enq_result.get("id")

            # 2. Run the autofix harness inline. The Coder has no timer by
            #    design (per AGENTIC_OPERATOR_PLAN.md) — the Operator's
            #    delegation IS the trigger.
            try:
                from src.research.autofix_dispatch import dispatch_symptom
                dispatch = dispatch_symptom(
                    db_path=db_path, symptom=symptom, context=context,
                )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "delegate_to_coder.dispatch_failed")
                dispatch = {"ok": False, "status": "error",
                            "reason": f"dispatch raised: {exc}"}

            # 3. Close out the agent_tasks row with the dispatch outcome so
            #    the Operator sees status=done/failed on next read.
            if task_id is not None:
                try:
                    with _connect(db_path) as conn:
                        status = "done" if dispatch.get("ok") else "failed"
                        conn.execute(
                            "UPDATE agent_tasks SET status = ?, "
                            "claimed_by = COALESCE(claimed_by, 'coder_inline'), "
                            "claimed_at_utc = COALESCE(claimed_at_utc, ?), "
                            "completed_at_utc = ?, result_json = ? "
                            "WHERE id = ?",
                            (status, _utc_now(), _utc_now(),
                             json.dumps(dispatch), int(task_id)),
                        )
                        conn.commit()
                except sqlite3.Error:
                    logging.getLogger(__name__).exception(
                        "delegate_to_coder.task_update_failed")

            enq_result["dispatch"] = dispatch
            return enq_result

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
    # Schemas for the repo-confined write/edit/shell tools.
    extra_schemas.append({
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a file inside this repo (creates parent dirs). Path is "
                "relative to the repo root, or absolute within it. REFUSED if it "
                "escapes the repo (other repos / system files)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative path"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    })
    extra_schemas.append({
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace a unique `old` substring with `new` in a repo file. "
                "Fails if `old` is missing or not unique. Repo-confined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    })
    extra_schemas.append({
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a shell command with the working directory set to this repo. "
                "Confined to the repo: commands referencing out-of-repo paths "
                "(other repos, /etc, home dirs) or '..' traversal are refused. "
                "Use for git, sqlite3 on judas_crew.db, running repo scripts/tests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["command"],
            },
        },
    })
    # Schema for flatten_position
    extra_schemas.append({
        "type": "function",
        "function": {
            "name": "flatten_position",
            "description": (
                "Close an existing futures position with a single MARKET order. "
                "No bracket children — exits cleanly without orphaning stop/"
                "target orders that would re-open the position. Use this for "
                "delegate_to_trader actions whose intent is to close a position."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["close_long", "close_short"]},
                    "qty": {"type": "integer", "minimum": 1},
                },
                "required": ["symbol", "side", "qty"],
            },
        },
    })
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
