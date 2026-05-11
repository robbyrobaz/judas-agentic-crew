"""CrewAI unified-Memory backend for the team's persistent findings.

Replaces the homebrew `findings` SQLite table with CrewAI's Memory class
(LanceDB vector store + ONNX embeddings + composite scoring on
semantic similarity / recency / importance).

The legacy `findings` table is kept read-only as a fallback so we don't
lose the handful of high-signal entries from before the cutover.

Key design choices (per Rob's mandate, 2026-05-11):
  - Storage: lancedb + onnx embedder (all-MiniLM-L6-v2). Single-machine.
  - importance_weight raised to 0.5 so human-authored memories surface
    even months later. semantic_weight=0.4, recency_weight=0.1.
  - All team memories live under scope='findings' so the existing
    record_finding/read_findings tool signatures stay unchanged.
  - Author-driven importance heuristics: human=0.98, discoveries=0.7,
    cycle-status pings=0.1. The agent prompts didn't expose an
    importance arg, so we infer.
  - Lazy singleton — avoid initialising LanceDB + ONNX on import.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MEMORY_DIR = _REPO_ROOT / "outputs" / "memory"
_SCOPE = "findings"

_MEMORY_LOCK = threading.Lock()
_MEMORY_SINGLETON: Any = None


def _get_memory():
    """Lazy-init the singleton Memory instance."""
    global _MEMORY_SINGLETON
    if _MEMORY_SINGLETON is not None:
        return _MEMORY_SINGLETON
    with _MEMORY_LOCK:
        if _MEMORY_SINGLETON is not None:
            return _MEMORY_SINGLETON
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("CREWAI_STORAGE_DIR", str(_MEMORY_DIR))
        from crewai.memory.unified_memory import Memory
        _MEMORY_SINGLETON = Memory(
            llm="minimax/MiniMax-M2.7",
            storage="lancedb",
            embedder={"provider": "onnx"},
            # Importance dominates so human guidance never gets buried
            # under registrar cycle-status pings.
            importance_weight=0.5,
            semantic_weight=0.4,
            recency_weight=0.1,
            recency_half_life_days=14,
        )
        log.info("memory_backend.initialized",
                 extra={"storage_dir": str(_MEMORY_DIR)})
    return _MEMORY_SINGLETON


# Cycle-status markers — the noise we want suppressed from recall.
_CYCLE_NOISE_PATTERNS = (
    re.compile(r"\bcycle\b.*\b(?:standing by|queue empty|no(?:\s|-)+ops?)\b",
               re.IGNORECASE),
    re.compile(r"\b\d+\s+mutations?\s+applied\b", re.IGNORECASE),
    re.compile(r"\baccount flat\b", re.IGNORECASE),
    re.compile(r"\bmarkets?\s+closed\b", re.IGNORECASE),
)


def _infer_importance(*, author: str, title: str, body: str) -> float:
    """Heuristic importance score in [0, 1].

    Human-authored memories: 0.98 (always surface).
    Cycle-status pings: 0.1 (suppress unless explicitly queried).
    Coder autofix completions: 0.5 (neutral).
    Everything else: 0.6 default — slightly above neutral to make
    agent-authored discoveries stand out.
    """
    a = (author or "").lower()
    t = (title or "")
    if a == "human":
        return 0.98
    blob = f"{t}\n{body or ''}"
    for pat in _CYCLE_NOISE_PATTERNS:
        if pat.search(blob):
            return 0.1
    if "coder autofix" in t.lower():
        return 0.5
    return 0.6


def save_memory(
    *,
    author: str,
    title: str,
    body: str,
    strategy_id: int | None = None,
    strategy_name: str | None = None,
    symbol: str | None = None,
    refs: dict | None = None,
    importance: float | None = None,
) -> dict:
    """Persist a finding into CrewAI Memory.

    Returns a dict matching the legacy record_finding shape:
      {ok, id, author, title, importance}
    """
    if not isinstance(title, str) or not title.strip():
        return {"ok": False, "error": "title required"}
    if not isinstance(body, str) or not body.strip():
        return {"ok": False, "error": "body required"}
    title = title.strip()
    body = body.strip()
    author = (author or "human").strip().lower()

    score = float(importance) if importance is not None else _infer_importance(
        author=author, title=title, body=body,
    )
    score = max(0.0, min(1.0, score))

    metadata: dict[str, Any] = {
        "title": title,
        "author": author,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if strategy_id is not None:
        metadata["strategy_id"] = int(strategy_id)
    if strategy_name:
        metadata["strategy_name"] = str(strategy_name)
    if symbol:
        metadata["symbol"] = str(symbol).upper()
    if refs:
        metadata["refs"] = refs

    # The content we embed = title + body. Title up-front so semantic
    # similarity catches keyword-ish queries too.
    content = f"{title}\n\n{body}"
    categories = [author, "finding"]
    if symbol:
        categories.append(symbol.upper())

    try:
        mem = _get_memory()
        rec = mem.remember(
            content=content,
            scope=_SCOPE,
            categories=categories,
            metadata=metadata,
            importance=score,
            source=author,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_backend.save_failed")
        return {"ok": False, "error": f"memory save failed: {exc}"}

    return {
        "ok": True,
        "id": rec.id if rec else None,
        "author": author,
        "title": title,
        "importance": score,
    }


def search_memory(
    *,
    query: str | None = None,
    author: str | None = None,
    symbol: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Retrieve findings ranked by composite score (importance + semantic
    + recency). Falls back to list_records when no query is given.

    Returns a list of dicts matching the legacy read_findings shape.
    """
    try:
        n = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        n = 20

    mem = _get_memory()
    out: list[dict] = []

    try:
        if query and query.strip():
            categories = [author] if author else None
            matches = mem.recall(
                query=query.strip(), scope=_SCOPE,
                categories=categories, limit=n,
            )
            for m in matches:
                out.append(_record_to_dict(m.record, score=m.score))
        else:
            # No query — list recent records, sorted by importance then
            # recency on our side. We oversample then trim.
            records = mem.list_records(scope=_SCOPE, limit=max(n * 3, 60))
            records.sort(
                key=lambda r: (
                    float(r.importance or 0),
                    str(r.created_at or ""),
                ),
                reverse=True,
            )
            if author:
                records = [r for r in records if (r.source or "").lower() == author.lower()]
            for r in records[:n]:
                out.append(_record_to_dict(r, score=None))
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_backend.search_failed")
        return []

    if symbol:
        sym_u = symbol.upper()
        out = [d for d in out if (d.get("symbol") or "").upper() == sym_u]

    return out[:n]


def forget_memory(*, record_id: str) -> dict:
    try:
        mem = _get_memory()
        removed = mem.forget(record_ids=[record_id])
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_backend.forget_failed")
        return {"ok": False, "error": f"forget failed: {exc}"}
    return {"ok": True, "removed": removed}


def _record_to_dict(rec: Any, *, score: float | None) -> dict:
    md = dict(rec.metadata or {})
    title = md.get("title") or (rec.content or "").split("\n", 1)[0][:120]
    body = rec.content or ""
    if title and body.startswith(title):
        body = body[len(title):].lstrip("\n")
    return {
        "id": str(rec.id),
        "created_at_utc": md.get("created_at_utc")
            or (rec.created_at.isoformat() if rec.created_at else ""),
        "author": rec.source or md.get("author") or "unknown",
        "title": title,
        "body": body,
        "strategy_id": md.get("strategy_id"),
        "strategy_name": md.get("strategy_name"),
        "symbol": md.get("symbol"),
        "refs_json": md.get("refs"),
        "importance": float(rec.importance or 0.0),
        "score": score,
        "status": "active",
    }


def drain() -> None:
    """Flush any pending async writes (call before shutdown / before
    a read that needs to see a write that just happened)."""
    try:
        mem = _get_memory()
        mem.drain_writes()
    except Exception:  # noqa: BLE001
        log.exception("memory_backend.drain_failed")
