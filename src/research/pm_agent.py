"""PM agent — loose-mandate elite-trader agent loop.

Per the Agentic Operator Plan: a single agent with the full mandate to make
money, equipped with a tool palette that wraps the existing code-enforced
deterministic paths (registry, broker, research). Hard safety stays in code:
this module never opens raw IBKR orders, never executes arbitrary shell or
Python, and never writes raw SQL to the database — every action goes through
an atomic helper that already enforces audit trails and paper-only mode.

Public surface:

    PMAction        — one tool invocation + its result
    PMDecisionResult — full outcome of a PM cycle
    run_pm_decision — entry point used by OperatorFlow.morning_review

Tests exercise this module by monkeypatching ``_call_llm`` (the litellm seam)
and ``_place_bracket`` (the broker seam). They never reach the network.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_ET_TZ = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Loose mandate — verbatim. Tests assert this is NOT re-narrowed with phrases
# like "validate before" or "every drop must" or "rolling waiting room".
# ---------------------------------------------------------------------------

PM_SYSTEM_PROMPT = """\
You are an elite futures trader with a $5,000 paper IBKR account.
Your only job is to make as much money as possible.

You have full access to all tools, live data, backtesting, the strategy
registry, and the ability to modify strategies, retire them, promote
new ones, and place your own paper trades when you see high-conviction
setups.

You can also reach outside the system for ideas:
  - web_search / web_fetch — read articles, papers, news, market commentary
  - fetch_youtube_transcript — ingest trader streams or talks (give a YouTube
    URL or video id, get the captions back as text)
  - read_file / list_files / read_research_artifact — browse research
    artifacts, prior briefs, the workshop's CSV leaderboards, anything
    inside this repo

You can invent novel strategies — not just configure the existing
RSI/MA/Bollinger/Judas families. Write a Python `evaluate(bars, params)`
function as a string, validate it via run_custom_backtest, then call
propose_custom_strategy to register it. The runtime will execute your
code in a sandboxed namespace (pandas, numpy, datetime only) on the
hourly scan once active.

Think like a profit-maximizing PM. Be decisive. Use tools aggressively.
Backtest when uncertain. Retire anything not contributing. Keep evolving
the system. The active set is not a portfolio to protect — it's a
rolling list of bets you've decided are worth running today.

You have {turn_budget} tool calls and {time_budget_s} seconds. End by
emitting a brief summary of what you did and why.
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class PMAction:
    action: str
    target_id: int | None
    payload: dict
    rationale: str
    tool_result: dict


@dataclass
class PMDecisionResult:
    success: bool
    actions_taken: list[PMAction]
    narrative: str
    turns_used: int
    elapsed_s: float
    fallback_used: bool
    raw_messages: list[dict] = field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Module-level seams — tests monkeypatch these
# ---------------------------------------------------------------------------


def _place_bracket(**kwargs) -> dict[str, Any]:
    """Broker seam — defers to portfolio_runtime.place_bracket.

    Tests monkeypatch this attribute on the module.
    """
    from src.portfolio_runtime import place_bracket as _impl

    return _impl(**kwargs)


# Tests can flip this to True to simulate dry-run paper mode.
_BROKER_DRY_RUN = False


_VALID_SYMBOLS = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}


_OUTPUT_ROW_CAP = 200
_OUTPUT_BYTES_CAP = 16 * 1024


# Repo root resolved once for path-safety checks. Tests can override via the
# ``JUDAS_REPO_ROOT`` env var.
def _repo_root() -> Path:
    override = os.environ.get("JUDAS_REPO_ROOT")
    if override:
        return Path(override).resolve()
    # src/research/pm_agent.py -> src/research -> src -> repo
    return Path(__file__).resolve().parents[2]


_FORBIDDEN_PATH_PREFIXES = ("/etc", "/proc", "/sys", "/dev", "/root")
_WEB_TIMEOUT_S = 15
_WEB_SNIPPET_CAP = 280
_WEB_FETCH_DEFAULT_CAP = 16_000
_YT_DEFAULT_CAP = 32_000


def _safe_resolve(path_str: str) -> tuple[Path | None, str | None]:
    """Resolve ``path_str`` against the repo root. Reject anything outside.

    Returns ``(resolved_path, None)`` on success, ``(None, error)`` otherwise.
    Code-level safety (per advisor): use ``Path.resolve()`` and
    ``.relative_to(repo_root)`` so symlinks, ``..``, and absolute paths are
    handled uniformly. Reject ``~`` BEFORE resolving since ``Path`` doesn't
    expand it but a string-match would miss surprises.
    """
    if not isinstance(path_str, str) or not path_str:
        return None, "path must be non-empty string"
    if path_str.startswith("~"):
        return None, "tilde paths not allowed"
    if any(path_str.startswith(p) for p in _FORBIDDEN_PATH_PREFIXES):
        return None, f"forbidden system path: {path_str}"
    root = _repo_root()
    p = Path(path_str)
    candidate = p if p.is_absolute() else (root / p)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"resolve failed: {exc}"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, "path resolves outside repo root"
    return resolved, None


# ---------------------------------------------------------------------------
# query_db safety — strip comments + leading whitespace, allow only one SELECT
# ---------------------------------------------------------------------------


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_sql_noise(sql: str) -> str:
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    return sql.strip()


def _is_safe_select(sql: str) -> tuple[bool, str | None]:
    if not isinstance(sql, str) or not sql.strip():
        return False, "empty sql"
    cleaned = _strip_sql_noise(sql)
    if not cleaned:
        return False, "empty after stripping comments"
    head = cleaned[:6].upper()
    if head != "SELECT":
        return False, "only SELECT statements are allowed"
    # Reject multi-statements: any non-whitespace / non-comment after a `;`
    if ";" in cleaned:
        # Permit only a trailing `;` with nothing meaningful after it.
        idx = cleaned.find(";")
        tail = _strip_sql_noise(cleaned[idx + 1 :])
        if tail:
            return False, "multi-statement queries are not allowed"
    # Reject pragmas + attaches even though SELECT-prefix would already block.
    lowered = cleaned.lower()
    for forbidden in ("pragma", "attach", "detach"):
        if re.search(rf"\b{forbidden}\b", lowered):
            return False, f"{forbidden} not allowed"
    return True, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Tool palette factory
# ---------------------------------------------------------------------------


def _safe_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool implementation so exceptions become {ok: false, error: ...}."""

    def wrapped(**kwargs) -> Any:
        try:
            return fn(**kwargs)
        except Exception as exc:  # noqa: BLE001 — defensive on the agent seam
            log.exception("pm_agent.tool.failed", extra={"tool": fn.__name__})
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    wrapped.__name__ = fn.__name__
    return wrapped


def _make_tools(*, db_path: str) -> dict[str, Callable[..., Any]]:
    """Build the tool palette bound to ``db_path``."""

    # ---- read tools ------------------------------------------------------

    def get_active_strategies() -> list[dict]:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, symbol, strategy_family, version, params_json, "
                "metrics_json, activated_at_utc, state "
                "FROM active_strategies WHERE state = 'active' ORDER BY id"
            ).fetchall()
        out: list[dict] = []
        now = datetime.now(timezone.utc)
        for r in rows:
            try:
                params = json.loads(r["params_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                params = {}
            try:
                metrics = json.loads(r["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            sid = int(r["id"])
            # Derive trade-level rolling metrics.
            with _connect(db_path) as conn2:
                tcount = conn2.execute(
                    "SELECT COUNT(*) AS n FROM trades WHERE strategy_id = ? AND status = 'closed'",
                    (sid,),
                ).fetchone()
                tsum = conn2.execute(
                    "SELECT COALESCE(SUM(pnl_dollars),0) AS s FROM trades WHERE strategy_id = ? AND status = 'closed'",
                    (sid,),
                ).fetchone()
                last = conn2.execute(
                    "SELECT MAX(opened_at) AS m FROM trades WHERE strategy_id = ?",
                    (sid,),
                ).fetchone()
            try:
                from src.research.live_review import compute_live_metrics

                lm = compute_live_metrics(db_path=db_path, strategy_id=sid)
                pf_20 = lm.pf_20
                expectancy_20 = lm.expectancy_20
                days_since_last_fire = lm.days_since_last_fire
            except Exception:  # noqa: BLE001
                pf_20 = None
                expectancy_20 = None
                days_since_last_fire = None
            try:
                act = datetime.fromisoformat(
                    str(r["activated_at_utc"]).replace("Z", "+00:00")
                )
                if act.tzinfo is None:
                    act = act.replace(tzinfo=timezone.utc)
                activated_days_ago = max(0, (now - act).days)
            except Exception:  # noqa: BLE001
                activated_days_ago = None
            out.append(
                {
                    "id": sid,
                    "symbol": str(r["symbol"]),
                    "strategy_family": str(r["strategy_family"]),
                    "version": int(r["version"]),
                    "strategy_name": params.get("strategy_name", ""),
                    "n_closed_trades": int(tcount["n"]),
                    "total_pnl": float(tsum["s"] or 0.0),
                    "pf_20": pf_20,
                    "expectancy_20": expectancy_20,
                    "days_since_last_fire": days_since_last_fire,
                    "activated_days_ago": activated_days_ago,
                }
            )
        return out

    def get_strategy_detail(*, id: int) -> dict:
        sid = int(id)
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ?", (sid,)
            ).fetchone()
        if row is None:
            return {"ok": False, "error": f"strategy {sid} not found"}
        d = _row_to_dict(row)
        try:
            d["params"] = json.loads(d.get("params_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["params"] = {}
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            d["metrics"] = {}
        return {"ok": True, "strategy": d}

    def get_workshop_leaderboard(*, limit: int = 20) -> list[dict]:
        from src.research.explore import _load_workshop_leaderboard_compact

        return _load_workshop_leaderboard_compact(limit=int(limit))

    def get_candidates_queue() -> list[dict]:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, ts_utc, symbol, strategy_family, status, decision, "
                "rationale FROM strategy_candidates WHERE status = 'candidate' "
                "ORDER BY ts_utc DESC LIMIT ?",
                (_OUTPUT_ROW_CAP,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_recent_pnl(*, days: int = 7) -> dict:
        days = int(days)
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT strategy_id, symbol, pnl_dollars, opened_at, closed_at, status
                FROM trades
                WHERE datetime(COALESCE(closed_at, opened_at)) >=
                      datetime('now', ?)
                """,
                (f"-{days} days",),
            ).fetchall()
        per_sid: dict[int, float] = {}
        per_day: dict[str, float] = {}
        total = 0.0
        for r in rows:
            pnl = float(r["pnl_dollars"] or 0.0)
            total += pnl
            sid = r["strategy_id"]
            if sid is not None:
                per_sid[int(sid)] = per_sid.get(int(sid), 0.0) + pnl
            day_key = str(r["closed_at"] or r["opened_at"] or "")[:10]
            if day_key:
                per_day[day_key] = per_day.get(day_key, 0.0) + pnl
        return {
            "total_pnl": total,
            "n_trades": len(rows),
            "per_strategy": [
                {"strategy_id": sid, "pnl": v} for sid, v in per_sid.items()
            ],
            "per_day": [{"date": d, "pnl": v} for d, v in sorted(per_day.items())],
        }

    def get_regime_tag() -> dict:
        from src.research.regime import tag_regime

        return tag_regime(db_path=db_path)

    def get_recent_briefs(*, limit: int = 3) -> list[dict]:
        limit = max(1, min(int(limit), 10))
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT brief_date, summary_json FROM daily_briefs "
                "ORDER BY brief_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                summary = json.loads(r["summary_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                summary = {}
            out.append({"brief_date": r["brief_date"], "summary": summary})
        return out

    def get_recent_experiments(*, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, ts_utc, symbol, experiment_type, name, status, summary "
                "FROM research_experiments ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_open_positions() -> list[dict]:
        """Return SLEEVE positions — what THIS system opened on the
        shared IBKR paper account.

        Reads live IBKR (clientId 199) AND the local agentic-crew
        ``trades`` table, then intersects. A live IBKR position is only
        included when there's a matching open trades row from this
        system. Workshop / options-recorder / any other tenant on the
        same paper account is filtered out so the Operator doesn't
        misread someone else's MBT or SPY position as ours.

        Each entry includes ``source='sleeve'`` and the local trade ids
        backing it for traceability.
        """
        # Local sleeve view first — agentic-crew's own open trades.
        sleeve_by_sym: dict[str, list[dict]] = {}
        try:
            with _connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT id, symbol, direction, qty, entry_fill, "
                    "stop_price, target_price, opened_at, ibkr_order_id "
                    "FROM trades WHERE status = 'open' "
                    "ORDER BY opened_at DESC LIMIT ?",
                    (_OUTPUT_ROW_CAP,),
                ).fetchall()
            for r in rows:
                sym = str(r["symbol"]).upper()
                sleeve_by_sym.setdefault(sym, []).append({
                    "trade_id": int(r["id"]),
                    "direction": str(r["direction"]),
                    "qty": int(r["qty"] or 0),
                    "entry_fill": r["entry_fill"],
                    "stop_price": r["stop_price"],
                    "target_price": r["target_price"],
                    "opened_at": r["opened_at"],
                    "ibkr_order_id": r["ibkr_order_id"],
                })
        except sqlite3.Error as exc:
            log.exception("get_open_positions.local_read_failed")
            return [{"ok": False, "error": f"sleeve DB read failed: {exc}",
                     "source": "sleeve_error"}]

        # Live IBKR view, filtered to symbols we have open sleeve trades for.
        if not sleeve_by_sym:
            return []  # Nothing in our sleeve — anything on IBKR is not ours.
        try:
            from ib_async import IB
            ib = IB()
            ib.connect("127.0.0.1", 4002, clientId=199, timeout=8)
            try:
                out: list[dict] = []
                for p in ib.positions():
                    if p.contract.secType != "FUT":
                        continue
                    if p.position == 0:
                        continue
                    sym = p.contract.symbol
                    if sym not in sleeve_by_sym:
                        # Position on the shared account but not ours.
                        continue
                    mult = float(p.contract.multiplier or 1)
                    avg_price = (p.avgCost or 0) / mult if mult else p.avgCost
                    out.append({
                        "symbol": sym,
                        "local_symbol": p.contract.localSymbol,
                        "direction": "long" if p.position > 0 else "short",
                        "qty": abs(int(p.position)),
                        "avg_price": round(float(avg_price), 4),
                        "contract_month": p.contract.lastTradeDateOrContractMonth,
                        "account": p.account,
                        "source": "sleeve",
                        "sleeve_trades": sleeve_by_sym.get(sym, []),
                    })
                return out
            finally:
                ib.disconnect()
        except Exception as exc:  # noqa: BLE001
            # IBKR unreachable — fall back to the local sleeve view.
            fallback: list[dict] = []
            for sym, trades in sleeve_by_sym.items():
                net = sum(t["qty"] if t["direction"] == "long" else -t["qty"]
                          for t in trades)
                if net == 0:
                    continue
                fallback.append({
                    "symbol": sym,
                    "direction": "long" if net > 0 else "short",
                    "qty": abs(net),
                    "source": "sleeve_db_fallback",
                    "sleeve_trades": trades,
                    "_ibkr_query_error": str(exc),
                })
            return fallback

    def query_db(*, sql: str) -> dict:
        ok, err = _is_safe_select(sql)
        if not ok:
            return {"ok": False, "error": err, "rows": []}
        try:
            with _connect(db_path) as conn:
                cur = conn.execute(sql)
                rows = cur.fetchmany(_OUTPUT_ROW_CAP)
                cols = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.Error as exc:
            return {"ok": False, "error": f"sqlite: {exc}", "rows": []}
        out_rows = [dict(zip(cols, r)) for r in rows]
        # Cap by serialized size.
        try:
            blob = json.dumps(out_rows, default=str)
        except (TypeError, ValueError):
            return {"ok": False, "error": "rows not json-serializable", "rows": []}
        if len(blob) > _OUTPUT_BYTES_CAP:
            # Trim until under cap.
            while out_rows and len(json.dumps(out_rows, default=str)) > _OUTPUT_BYTES_CAP:
                out_rows.pop()
        return {"ok": True, "rows": out_rows, "n_rows": len(out_rows)}

    # ---- web / external knowledge ---------------------------------------

    def web_search(*, query: str, max_results: int = 5) -> dict:
        if not isinstance(query, str) or not query.strip():
            return {"ok": False, "error": "query required"}
        try:
            n = int(max_results)
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(n, 20))
        try:
            try:
                from ddgs import DDGS  # type: ignore[import-not-found]
            except ImportError:
                try:
                    from duckduckgo_search import DDGS  # type: ignore[import-not-found]
                except ImportError:
                    return {"ok": False, "error": "duckduckgo-search not installed"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"ddg import failed: {exc}"}
        try:
            with DDGS() as ddg:
                raw = list(ddg.text(query, max_results=n))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"ddg search failed: {exc}"}
        out = []
        for r in raw[:n]:
            title = str(r.get("title") or "")[:200]
            url = str(r.get("href") or r.get("url") or "")
            snippet = str(r.get("body") or r.get("snippet") or "")
            if len(snippet) > _WEB_SNIPPET_CAP:
                snippet = snippet[:_WEB_SNIPPET_CAP]
            out.append({"title": title, "url": url, "snippet": snippet})
        return {"ok": True, "results": out}

    def web_fetch(*, url: str, max_chars: int = _WEB_FETCH_DEFAULT_CAP) -> dict:
        if not isinstance(url, str) or not url.strip():
            return {"ok": False, "error": "url required"}
        u = url.strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return {"ok": False, "error": "only http(s) URLs allowed"}
        try:
            cap = int(max_chars)
        except (TypeError, ValueError):
            cap = _WEB_FETCH_DEFAULT_CAP
        cap = max(1024, min(cap, 200_000))
        try:
            import requests  # type: ignore[import-not-found]
        except ImportError:
            return {"ok": False, "error": "requests not installed"}
        try:
            resp = requests.get(
                u, timeout=_WEB_TIMEOUT_S,
                headers={"User-Agent": "judas-pm-agent/1.0"},
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"http failed: {exc}"}
        ctype = resp.headers.get("content-type", "")
        text = resp.text or ""
        if "html" in ctype.lower() or text.lstrip().startswith("<"):
            try:
                from bs4 import BeautifulSoup  # type: ignore[import-not-found]
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n").strip()
                # Collapse runs of whitespace
                text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            except Exception as exc:  # noqa: BLE001
                log.warning("bs4 parse failed: %s", exc)
        if len(text) > cap:
            text = text[:cap]
        return {
            "ok": True,
            "status_code": int(resp.status_code),
            "content_type": ctype,
            "text": text,
        }

    _YT_ID_RX = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")

    def fetch_youtube_transcript(*, url_or_id: str, max_chars: int = _YT_DEFAULT_CAP) -> dict:
        if not isinstance(url_or_id, str) or not url_or_id.strip():
            return {"ok": False, "error": "url_or_id required"}
        s = url_or_id.strip()
        try:
            cap = int(max_chars)
        except (TypeError, ValueError):
            cap = _YT_DEFAULT_CAP
        cap = max(1024, min(cap, 500_000))
        # Bare ID?
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
            video_id = s
        else:
            m = _YT_ID_RX.search(s)
            if not m:
                return {"ok": False, "error": "could not parse youtube video id"}
            video_id = m.group(1)
        try:
            from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore[import-not-found]
        except ImportError:
            return {"ok": False, "error": "youtube-transcript-api not installed"}
        try:
            api = YouTubeTranscriptApi()
            try:
                fetched = api.fetch(video_id)
            except AttributeError:
                # Older API: classmethod get_transcript
                fetched = YouTubeTranscriptApi.get_transcript(video_id)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"youtube fetch failed: {exc}"}

        # Normalize: instance fetch returns FetchedTranscript (iterable of
        # FetchedTranscriptSnippet with .text); legacy returns list[dict].
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
            "transcript": transcript,
            "truncated": truncated,
        }

    # ---- peer filesystem reads ------------------------------------------

    def read_file(*, path: str, max_bytes: int = 65536) -> dict:
        resolved, err = _safe_resolve(path)
        if err:
            return {"ok": False, "error": err}
        try:
            cap = int(max_bytes)
        except (TypeError, ValueError):
            cap = 65536
        cap = max(256, min(cap, 1_000_000))
        if not resolved.exists():
            return {"ok": False, "error": f"path does not exist: {path}"}
        if not resolved.is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            return {"ok": False, "error": f"read failed: {exc}"}
        truncated = False
        if len(raw) > cap:
            raw = raw[:cap]
            truncated = True
        # Binary heuristic: presence of NUL byte in the first 8KB.
        if b"\x00" in raw[:8192]:
            return {"ok": False, "error": "binary file rejected"}
        try:
            content = raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"decode failed: {exc}"}
        return {
            "ok": True,
            "path": str(resolved.relative_to(_repo_root())),
            "content": content,
            "truncated": truncated,
        }

    def list_files(*, path: str = ".", glob: str = "**/*", max_results: int = 200) -> dict:
        resolved, err = _safe_resolve(path)
        if err:
            return {"ok": False, "error": err}
        if not resolved.exists() or not resolved.is_dir():
            return {"ok": False, "error": f"not a directory: {path}"}
        try:
            cap = int(max_results)
        except (TypeError, ValueError):
            cap = 200
        cap = max(1, min(cap, 1000))
        out: list[dict] = []
        root = _repo_root()
        try:
            for entry in resolved.glob(glob):
                if not entry.is_file():
                    continue
                try:
                    entry_resolved = entry.resolve()
                    entry_resolved.relative_to(root)
                except (ValueError, OSError):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                out.append({
                    "path": str(entry_resolved.relative_to(root)),
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                })
                if len(out) >= cap:
                    break
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"glob failed: {exc}"}
        return {"ok": True, "files": out}

    def read_research_artifact(*, experiment_id: int, kind: str = "json") -> dict:
        try:
            eid = int(experiment_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "experiment_id must be int"}
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT artifacts_json FROM research_experiments WHERE id = ?",
                (eid,),
            ).fetchone()
        if row is None:
            return {"ok": False, "error": f"experiment {eid} not found"}
        try:
            artifacts = json.loads(row["artifacts_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return {"ok": False, "error": "artifacts_json malformed"}
        if not isinstance(artifacts, dict):
            return {"ok": False, "error": "artifacts not a dict"}
        target = artifacts.get(kind)
        if not target:
            return {"ok": False, "error": f"no '{kind}' artifact for experiment {eid}",
                    "available_kinds": sorted(artifacts.keys())}
        # Reuse read_file for path-safety + cap.
        sub = read_file(path=str(target))
        if not sub.get("ok"):
            return sub
        return {"ok": True, "path": sub.get("path"), "content": sub.get("content")}

    # ---- backtesting tools ----------------------------------------------

    def run_judas_threshold_sweep(*, symbol: str, **kwargs) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        from src.tools import research_tools as rt

        payload = {"symbol": sym, **kwargs}
        try:
            raw = rt.judas_parameter_sweep_tool.run(input_json=json.dumps(payload, default=str))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"sweep failed: {exc}"}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"ok": False, "error": "sweep returned non-JSON", "raw": str(raw)[:400]}
        # Cap top results.
        if isinstance(data, dict):
            for k in ("top", "results", "top_results"):
                v = data.get(k)
                if isinstance(v, list) and len(v) > 10:
                    data[k] = v[:10]
        return {"ok": True, "result": data}

    def run_walk_forward(*, symbol: str, params: dict | None = None) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        from src.tools import research_tools as rt

        payload: dict[str, Any] = {"symbol": sym}
        if params:
            payload["parameters"] = params
        try:
            raw = rt.walk_forward_tool.run(input_json=json.dumps(payload, default=str))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"walk_forward failed: {exc}"}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"ok": False, "error": "walk_forward returned non-JSON"}
        return {"ok": True, "result": data}

    # ---- action tools (atomic, code-enforced) ----------------------------

    def retire_strategy_tool(*, id: int, reason: str) -> dict:
        from src import strategy_registry

        sid = int(id)
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason required"}
        # Snapshot metrics before retire.
        snap: dict = {}
        try:
            from src.research.live_review import compute_live_metrics

            m = compute_live_metrics(db_path=db_path, strategy_id=sid)
            snap = {
                "pf_20": m.pf_20,
                "expectancy_20": m.expectancy_20,
                "n_closed_trades": m.n_closed_trades,
                "total_realized_pnl": m.total_realized_pnl,
            }
        except Exception:  # noqa: BLE001
            snap = {}
        try:
            demotion_id = strategy_registry.retire_strategy(
                strategy_id=sid, reason=reason, metrics_snapshot=snap,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "demotion_id": int(demotion_id)}

    def propose_candidate_tool(
        *,
        symbol: str,
        strategy_family: str,
        params: dict,
        metrics: dict | None = None,
        rationale: str = "",
    ) -> dict:
        from src import strategy_registry

        sym = str(symbol).upper()
        if not isinstance(params, dict) or "symbol" not in params:
            params = dict(params or {})
            params["symbol"] = sym
        try:
            cid = strategy_registry.create_candidate(
                symbol=sym,
                strategy_family=str(strategy_family),
                params=dict(params),
                metrics=dict(metrics or {}),
                decision="pm_agent_propose",
                rationale=str(rationale or ""),
                status="candidate",
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "candidate_id": int(cid)}

    def promote_candidate_tool(*, id: int, notes: str = "") -> dict:
        from src import strategy_registry

        cid = int(id)
        try:
            new = strategy_registry.promote_candidate(cid, notes=notes or None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "new_strategy_id": int(new.id), "version": int(new.version)}

    def insert_active_strategy_tool(
        *, symbol: str, strategy_family: str,
        params: dict | None = None, notes: str = "",
    ) -> dict:
        """Insert a brand-new active_strategies row from scratch.

        Use this when no candidate exists yet (or you want to seed a new
        (symbol, family) pair directly). Multiple actives per pair are
        allowed; this does NOT retire anything.
        """
        from src import strategy_registry
        try:
            new = strategy_registry.insert_active_strategy(
                symbol=symbol, strategy_family=strategy_family,
                params=params, notes=notes or None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "new_strategy_id": int(new.id),
                "version": int(new.version), "symbol": new.symbol,
                "strategy_family": new.strategy_family}

    def modify_strategy_params_tool(
        *, id: int, new_params: dict, rationale: str
    ) -> dict:
        """Atomic retire+promote in a single transaction.

        Per the brief and advisor review: ``promote_candidate`` computes
        ``next_version`` from the currently-active row only, so the naïve
        retire-then-promote sequence yields ``version=1``. We do a single
        explicit transaction here that retires the old row, writes an
        ``auto_demotions`` audit row, and inserts a new ``active_strategies``
        row at ``old.version + 1`` with the new params. A
        ``strategy_candidates`` row is written first with status='promoted'
        so the audit trail also shows the modify.
        """
        sid = int(id)
        if not isinstance(new_params, dict):
            return {"ok": False, "error": "new_params must be dict"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}

        from src.db.models import init_db

        init_db(db_path)

        # Snapshot live metrics outside the transaction (read-only).
        snap: dict = {}
        try:
            from src.research.live_review import compute_live_metrics

            m = compute_live_metrics(db_path=db_path, strategy_id=sid)
            snap = {
                "pf_20": m.pf_20,
                "expectancy_20": m.expectancy_20,
                "n_closed_trades": m.n_closed_trades,
                "total_realized_pnl": m.total_realized_pnl,
            }
        except Exception:  # noqa: BLE001
            snap = {}

        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ? AND state = 'active'",
                (sid,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return {
                    "ok": False,
                    "error": f"active strategy {sid} not found or not active",
                }
            symbol = str(row["symbol"])
            family = str(row["strategy_family"])
            old_version = int(row["version"])
            old_params_json = str(row["params_json"] or "{}")
            now = _utc_now()

            # Ensure new_params has identifying keys for validation parity.
            merged = dict(new_params)
            merged.setdefault("symbol", symbol)
            try:
                old_params = json.loads(old_params_json)
            except (TypeError, json.JSONDecodeError):
                old_params = {}
            for alt in ("strategy_name", "strategy_family"):
                if alt not in merged and alt in old_params:
                    merged[alt] = old_params[alt]

            # 1) Audit: write candidate row showing the modify intent.
            cur = conn.execute(
                """
                INSERT INTO strategy_candidates
                    (ts_utc, symbol, strategy_family, source_experiment_id,
                     params_json, metrics_json, decision, rationale, status)
                VALUES (?, ?, ?, NULL, ?, ?, 'pm_agent_modify', ?, 'promoted-from-modify')
                """,
                (
                    now,
                    symbol,
                    family,
                    json.dumps(merged, default=str),
                    json.dumps(snap, default=str),
                    rationale,
                ),
            )
            candidate_id = int(cur.lastrowid)

            # 2) Insert new active row at old_version + 1 BEFORE retiring old,
            #    so external readers never see a zero-active window.
            cur = conn.execute(
                """
                INSERT INTO active_strategies
                    (symbol, strategy_family, version, params_json, metrics_json,
                     source_candidate_id, state, activated_at_utc, notes)
                VALUES (?, ?, ?, ?, '{}', ?, 'active', ?, ?)
                """,
                (
                    symbol,
                    family,
                    old_version + 1,
                    json.dumps(merged, default=str),
                    candidate_id,
                    now,
                    f"PM agent modify: {rationale}",
                ),
            )
            new_strategy_id = int(cur.lastrowid)

            # 3) Retire old active row.
            conn.execute(
                """
                UPDATE active_strategies
                SET state = 'retired',
                    deactivated_at_utc = ?,
                    notes = COALESCE(notes, '') || ?
                WHERE id = ?
                """,
                (now, f"\nAuto-modified: {rationale}", sid),
            )

            # 4) Auto_demotions audit row preserves the snapshot.
            cur = conn.execute(
                """
                INSERT INTO auto_demotions
                    (ts_utc, strategy_id, symbol, strategy_family, version,
                     params_json, metrics_snapshot_json, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    sid,
                    symbol,
                    family,
                    old_version,
                    old_params_json,
                    json.dumps(snap, default=str),
                    f"pm_agent_modify: {rationale}",
                ),
            )
            demotion_id = int(cur.lastrowid)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            conn.close()
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        return {
            "ok": True,
            "demotion_id": demotion_id,
            "new_strategy_id": new_strategy_id,
            "version": old_version + 1,
        }

    # ---- custom strategy invention --------------------------------------

    def run_custom_backtest_tool(*, code: str, symbol: str, days: int = 90) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "code required"}
        from src.research import custom_strategy_runtime as csr

        try:
            metrics = csr.run_custom_backtest(
                code=code, symbol=sym, days=int(days), db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"backtest failed: {type(exc).__name__}: {exc}"}
        return {"ok": True, "metrics": metrics}

    def propose_custom_strategy_tool(
        *,
        name: str,
        symbol: str,
        code: str,
        rationale: str,
        backtest_metrics: dict | None = None,
    ) -> dict:
        from src.db.models import init_db as _init
        from src.research import custom_strategy_runtime as csr

        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "name required"}
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "code required"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}
        # Validate compilation + presence of evaluate.
        fn, meta = csr._compile_and_load(code)
        if fn is None:
            return {"ok": False, "error": f"code validation failed: {meta.get('error')}"}
        _init(db_path)
        conn = _connect(db_path)
        try:
            existing = conn.execute(
                "SELECT id FROM custom_strategies WHERE name = ? AND retired_at_utc IS NULL",
                (name,),
            ).fetchone()
            if existing is not None:
                return {"ok": False, "error": f"name already in use: {name}"}
            cur = conn.execute(
                """
                INSERT INTO custom_strategies
                    (created_at_utc, name, symbol, code, rationale,
                     backtest_metrics_json, active, retired_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, 1, NULL)
                """,
                (
                    _utc_now(), name, sym, code, rationale,
                    json.dumps(backtest_metrics or {}, default=str),
                ),
            )
            cid = int(cur.lastrowid)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            return {"ok": False, "error": f"db integrity: {exc}"}
        finally:
            conn.close()
        return {"ok": True, "custom_strategy_id": cid}

    def list_custom_strategies_tool() -> list[dict]:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, created_at_utc, name, symbol, rationale, "
                "backtest_metrics_json, active "
                "FROM custom_strategies "
                "WHERE active = 1 AND retired_at_utc IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (_OUTPUT_ROW_CAP,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            try:
                m = json.loads(r["backtest_metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                m = {}
            out.append({
                "id": int(r["id"]),
                "created_at_utc": str(r["created_at_utc"]),
                "name": str(r["name"]),
                "symbol": str(r["symbol"]),
                "rationale": str(r["rationale"]),
                "backtest_metrics": m,
                "active": int(r["active"]),
            })
        return out

    def retire_custom_strategy_tool(*, id: int, reason: str) -> dict:
        try:
            cid = int(id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be int"}
        if not isinstance(reason, str) or not reason.strip():
            return {"ok": False, "error": "reason required"}
        from src.db.models import init_db as _init
        _init(db_path)
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT id, name, symbol FROM custom_strategies "
                "WHERE id = ? AND active = 1 AND retired_at_utc IS NULL",
                (cid,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": f"custom strategy {cid} not active"}
            now = _utc_now()
            conn.execute(
                "UPDATE custom_strategies SET active = 0, retired_at_utc = ? WHERE id = ?",
                (now, cid),
            )
            cur = conn.execute(
                """
                INSERT INTO auto_demotions
                    (ts_utc, strategy_id, symbol, strategy_family, version,
                     params_json, metrics_snapshot_json, reason)
                VALUES (?, ?, ?, 'custom', 1, ?, ?, ?)
                """,
                (
                    now, cid, str(row["symbol"]),
                    json.dumps({"custom_strategy_id": cid, "name": str(row["name"])}),
                    "{}",
                    f"retire_custom_strategy: {reason}",
                ),
            )
            demotion_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "custom_strategy_id": cid, "demotion_id": demotion_id}

    def place_paper_order(
        *,
        symbol: str,
        side: str,
        quantity: int,
        stop_price: float,
        target_price: float,
        rationale: str,
    ) -> dict:
        sym = str(symbol).upper()
        if sym not in _VALID_SYMBOLS:
            return {"ok": False, "error": f"unknown symbol: {sym}"}
        side_u = str(side).upper()
        if side_u not in ("BUY", "SELL"):
            return {"ok": False, "error": "side must be BUY or SELL"}
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            return {"ok": False, "error": "quantity must be int"}
        if qty <= 0:
            return {"ok": False, "error": "quantity must be > 0"}
        try:
            stop = float(stop_price)
            tgt = float(target_price)
        except (TypeError, ValueError):
            return {"ok": False, "error": "stop_price/target_price must be numeric"}
        if not isinstance(rationale, str) or not rationale.strip():
            return {"ok": False, "error": "rationale required"}

        if _BROKER_DRY_RUN:
            log.info("pm_agent.place_paper_order.dry_run", extra={"symbol": sym})
            return {"ok": False, "reason": "dry_run"}

        # Insert signals row first so the trade is attributable even on broker
        # failure.
        direction = "long" if side_u == "BUY" else "short"
        from src.db.models import init_db

        init_db(db_path)
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO signals
                    (ts_utc, symbol, strategy_id, strategy_family, strategy_version,
                     direction, quality_score, risk_decision, entry, stop, target,
                     rationale, agent_notes, raw_llm_output, created_at)
                VALUES (?, ?, NULL, 'pm_agent', NULL, ?, NULL, 'PM_AGENT', NULL, ?, ?,
                        ?, ?, NULL, ?)
                """,
                (
                    _utc_now(),
                    sym,
                    direction,
                    stop,
                    tgt,
                    rationale,
                    json.dumps({"source": "pm_agent"}),
                    _utc_now(),
                ),
            )
            signal_id = int(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()

        # Resolve broker config and place via the deterministic seam.
        try:
            from src.config import load_config

            cfg = load_config()
            ibkr = cfg.ibkr
            order = _place_bracket(
                symbol=sym,
                side=side_u,
                quantity=qty,
                stop_price=stop,
                target_price=tgt,
                host=ibkr.host,
                port=ibkr.port,
                client_id=ibkr.exec_client_id,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"broker failed: {type(exc).__name__}: {exc}",
                "signal_id": signal_id,
            }

        return {
            "ok": True,
            "signal_id": signal_id,
            "ibkr_order_ids": [
                order.get("parent_order_id"),
                order.get("tp_order_id"),
                order.get("sl_order_id"),
            ],
            "order": order,
        }

    return {
        "get_active_strategies": _safe_tool(get_active_strategies),
        "get_strategy_detail": _safe_tool(get_strategy_detail),
        "get_workshop_leaderboard": _safe_tool(get_workshop_leaderboard),
        "get_candidates_queue": _safe_tool(get_candidates_queue),
        "get_recent_pnl": _safe_tool(get_recent_pnl),
        "get_regime_tag": _safe_tool(get_regime_tag),
        "get_recent_briefs": _safe_tool(get_recent_briefs),
        "get_recent_experiments": _safe_tool(get_recent_experiments),
        "get_open_positions": _safe_tool(get_open_positions),
        "query_db": _safe_tool(query_db),
        "run_judas_threshold_sweep": _safe_tool(run_judas_threshold_sweep),
        "run_walk_forward": _safe_tool(run_walk_forward),
        "retire_strategy": _safe_tool(retire_strategy_tool),
        "propose_candidate": _safe_tool(propose_candidate_tool),
        "promote_candidate": _safe_tool(promote_candidate_tool),
        "insert_active_strategy": _safe_tool(insert_active_strategy_tool),
        "modify_strategy_params": _safe_tool(modify_strategy_params_tool),
        "place_paper_order": _safe_tool(place_paper_order),
        # Phase 9 — external knowledge
        "web_search": _safe_tool(web_search),
        "web_fetch": _safe_tool(web_fetch),
        "fetch_youtube_transcript": _safe_tool(fetch_youtube_transcript),
        # Phase 9 — peer filesystem
        "read_file": _safe_tool(read_file),
        "list_files": _safe_tool(list_files),
        "read_research_artifact": _safe_tool(read_research_artifact),
        # Phase 9 — custom strategy invention
        "run_custom_backtest": _safe_tool(run_custom_backtest_tool),
        "propose_custom_strategy": _safe_tool(propose_custom_strategy_tool),
        "list_custom_strategies": _safe_tool(list_custom_strategies_tool),
        "retire_custom_strategy": _safe_tool(retire_custom_strategy_tool),
    }


# Action tools whose invocations populate PMDecisionResult.actions_taken.
_ACTION_TOOL_NAMES = {
    "retire_strategy",
    "propose_candidate",
    "promote_candidate",
    "modify_strategy_params",
    "place_paper_order",
    "run_judas_threshold_sweep",
    "run_walk_forward",
    "run_custom_backtest",
    "propose_custom_strategy",
    "retire_custom_strategy",
}


def _tool_schemas() -> list[dict]:
    sym_enum = sorted(_VALID_SYMBOLS)
    return [
        {
            "type": "function",
            "function": {
                "name": "get_active_strategies",
                "description": "List active strategies with rolling metrics.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_strategy_detail",
                "description": "Full params + metrics for one active strategy id.",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_workshop_leaderboard",
                "description": "Top-N strategies from knowledge_base/buffet.yaml.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 20}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_candidates_queue",
                "description": "Pending strategy_candidates rows.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_pnl",
                "description": "Realized P&L total + per-strategy + per-day.",
                "parameters": {
                    "type": "object",
                    "properties": {"days": {"type": "integer", "default": 7}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_regime_tag",
                "description": "Coarse market regime tag (vol_regime/trend/leaders).",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_briefs",
                "description": "Last N daily briefs.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 3}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_experiments",
                "description": "Recent research_experiments rows.",
                "parameters": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "default": 10}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_open_positions",
                "description": "DB-recorded open trades.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_db",
                "description": (
                    "Run a single SELECT statement (read-only). "
                    "Multi-statement, PRAGMA, ATTACH, and writes are rejected."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_judas_threshold_sweep",
                "description": "Backtest Judas thresholds for a symbol.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "max_combinations": {"type": "integer"},
                        "min_trades": {"type": "integer"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_walk_forward",
                "description": "Walk-forward validation for a symbol+params.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "params": {"type": "object"},
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retire_strategy",
                "description": (
                    "Atomically retire an active strategy. Writes auto_demotions audit row."
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
                "name": "propose_candidate",
                "description": "Insert a strategy_candidates row with status=candidate.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "enum": sym_enum},
                        "strategy_family": {"type": "string"},
                        "params": {"type": "object"},
                        "metrics": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["symbol", "strategy_family", "params", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "promote_candidate",
                "description": "Atomically promote a candidate to active (v+1).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "notes": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "insert_active_strategy",
                "description": (
                    "Create a brand-new active_strategies row from scratch. "
                    "Use when no candidate exists or you want to seed a new "
                    "(symbol, strategy_family) pair directly. Multiple "
                    "actives per pair are allowed; this does NOT retire."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "strategy_family": {"type": "string"},
                        "params": {"type": "object"},
                        "notes": {"type": "string"},
                    },
                    "required": ["symbol", "strategy_family"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "modify_strategy_params",
                "description": (
                    "Atomic retire-old + promote-new with new params. "
                    "Writes auto_demotions audit and a v+1 active row."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "new_params": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["id", "new_params", "rationale"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "place_paper_order",
                "description": (
                    "Place a paper bracket via the deterministic broker path. "
                    "Inserts a signals row first; returns ibkr_order_ids on success."
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
        # ---- Phase 9: external knowledge ---------------------------------
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "DuckDuckGo search; returns title/url/snippet (snippets capped 280 chars).",
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
                "name": "web_fetch",
                "description": "Fetch an http(s) URL; HTML auto-extracted to text. Capped to max_chars.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 16000},
                    },
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_youtube_transcript",
                "description": "Fetch transcript for a YouTube URL or 11-char id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url_or_id": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 32000},
                    },
                    "required": ["url_or_id"],
                },
            },
        },
        # ---- Phase 9: peer filesystem ------------------------------------
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from inside the repo. Binary rejected.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_bytes": {"type": "integer", "default": 65536},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files under a repo-relative path matching glob.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "glob": {"type": "string", "default": "**/*"},
                        "max_results": {"type": "integer", "default": 200},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_research_artifact",
                "description": "Read an artifact attached to a research_experiments row.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "experiment_id": {"type": "integer"},
                        "kind": {"type": "string", "default": "json"},
                    },
                    "required": ["experiment_id"],
                },
            },
        },
        # ---- Phase 9: custom strategy invention --------------------------
        {
            "type": "function",
            "function": {
                "name": "run_custom_backtest",
                "description": (
                    "Run a sandboxed backtest of agent-authored Python "
                    "strategy code (must define evaluate(bars, params))."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "symbol": {"type": "string", "enum": sym_enum},
                        "days": {"type": "integer", "default": 90},
                    },
                    "required": ["code", "symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_custom_strategy",
                "description": "Register a new custom strategy from agent-authored code.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "symbol": {"type": "string", "enum": sym_enum},
                        "code": {"type": "string"},
                        "rationale": {"type": "string"},
                        "backtest_metrics": {"type": "object"},
                    },
                    "required": ["name", "symbol", "code", "rationale", "backtest_metrics"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_custom_strategies",
                "description": "List active custom strategies registered by the agent.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "retire_custom_strategy",
                "description": "Mark a custom strategy retired (writes auto_demotions audit row).",
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
    ]


# ---------------------------------------------------------------------------
# litellm seam
# ---------------------------------------------------------------------------


def _call_llm(
    *, messages: list[dict], tools: list[dict], model: str, timeout_s: int
) -> Any:
    """Wrap litellm so tests can monkeypatch this single seam."""
    import litellm  # type: ignore[import-not-found]

    return litellm.completion(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        timeout=timeout_s,
        temperature=0.2,
    )


def _extract_message(response: Any) -> dict:
    try:
        choice = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        choice = getattr(response, "choices", [None])[0]
        choice = getattr(choice, "message", None) or {}
    if hasattr(choice, "model_dump"):
        return choice.model_dump()
    if hasattr(choice, "to_dict"):
        return choice.to_dict()
    if isinstance(choice, dict):
        return dict(choice)
    return {"role": "assistant", "content": str(choice)}


def _tool_calls_from(message: dict) -> list[dict]:
    raw = message.get("tool_calls") or []
    out: list[dict] = []
    for tc in raw:
        if isinstance(tc, dict):
            out.append(tc)
        elif hasattr(tc, "model_dump"):
            out.append(tc.model_dump())
        elif hasattr(tc, "to_dict"):
            out.append(tc.to_dict())
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_pm_decision(
    *,
    db_path: str,
    turn_budget: int = 0,
    time_budget_s: int = 0,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> PMDecisionResult:
    """Run one PM cycle. Pure function over ``db_path`` plus the LLM seam.

    No-key fallback: when ``MINIMAX_API_KEY`` is missing this returns
    immediately with ``fallback_used=True`` and zero actions — by design.
    The operator's daily check picks up the no-op narrative.
    """
    started = time.time()

    if os.environ.get("JUDAS_PM_AGENT_INHIBIT") == "1":
        log.info("pm_agent.inhibited_by_env")
        return PMDecisionResult(
            success=True,
            actions_taken=[],
            narrative="PM agent inhibited via JUDAS_PM_AGENT_INHIBIT (test/safety mode); no actions taken.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    if not os.environ.get("MINIMAX_API_KEY"):
        log.warning("pm_agent.no_api_key.fallback_noop")
        return PMDecisionResult(
            success=True,
            actions_taken=[],
            narrative="M2.7 unreachable (MINIMAX_API_KEY missing); no actions taken today.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    tools = _make_tools(db_path=db_path)
    schemas = _tool_schemas()

    _tb = "unlimited" if not turn_budget or turn_budget <= 0 else str(turn_budget)
    _ts = "unlimited" if not time_budget_s or time_budget_s <= 0 else f"{time_budget_s}s"
    system_prompt = PM_SYSTEM_PROMPT.format(turn_budget=_tb, time_budget_s=_ts)
    try:
        date_et = datetime.now(_ET_TZ).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        date_et = datetime.now(timezone.utc).isoformat()
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"It's {date_et}. Trade."},
    ]

    actions: list[PMAction] = []
    turn = 0
    error: str | None = None
    final_text = ""

    # turn_budget <= 0 disables the turn cap; same for time_budget_s.
    unlimited_turns = turn_budget is None or turn_budget <= 0
    unlimited_time = time_budget_s is None or time_budget_s <= 0
    while unlimited_turns or turn < turn_budget:
        elapsed = time.time() - started
        if not unlimited_time and elapsed >= time_budget_s:
            error = f"time budget exhausted after {elapsed:.1f}s"
            break
        if unlimited_time:
            remaining = 300
        else:
            remaining = max(1, int(time_budget_s - elapsed))
        try:
            response = _call_llm(
                messages=messages,
                tools=schemas,
                model=minimax_model,
                timeout_s=min(remaining, 300),
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            error = f"llm call failed: {exc}"
            break

        msg = _extract_message(response)
        messages.append(msg)
        turn += 1

        tool_calls = _tool_calls_from(msg)
        if not tool_calls:
            final_text = str(msg.get("content") or "").strip()
            break

        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            arg_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(arg_str) if isinstance(arg_str, str) else dict(arg_str)
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_fn = tools.get(name)
            if tool_fn is None:
                result: Any = {"ok": False, "error": f"unknown tool {name!r}"}
            else:
                try:
                    result = tool_fn(**args)
                except TypeError as exc:
                    result = {"ok": False, "error": f"bad args: {exc}"}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": f"tool failed: {exc}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name,
                    "content": json.dumps(result, default=str),
                }
            )
            if name in _ACTION_TOOL_NAMES:
                target_id = None
                if isinstance(args.get("id"), int):
                    target_id = args["id"]
                actions.append(
                    PMAction(
                        action=name,
                        target_id=target_id,
                        payload=dict(args),
                        rationale=str(args.get("rationale") or args.get("reason") or args.get("notes") or ""),
                        tool_result=result if isinstance(result, dict) else {"value": result},
                    )
                )

    elapsed_s = time.time() - started
    if not final_text:
        # Best-effort: stitch a narrative from the last assistant message + actions.
        last_assistant = next(
            (m.get("content") for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        if last_assistant:
            final_text = str(last_assistant).strip()
        else:
            final_text = (
                f"PM cycle ended with {len(actions)} action(s); turns={turn}, "
                f"elapsed={elapsed_s:.1f}s."
            )

    return PMDecisionResult(
        success=error is None,
        actions_taken=actions,
        narrative=final_text,
        turns_used=turn,
        elapsed_s=elapsed_s,
        fallback_used=False,
        raw_messages=messages,
        error=error,
    )
