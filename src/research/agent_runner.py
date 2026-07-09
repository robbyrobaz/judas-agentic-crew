"""Shared agent loop used by Operator + four specialists.

Each agent module supplies its system prompt + tool include-set. The
loop here drives the litellm seam, executes tool calls, and records
actions. Tests monkeypatch ``_call_llm`` to inject scripted responses.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from src.research import agent_tools, pm_agent as _pm
from src.research.pm_agent import _extract_message, _tool_calls_from


def _call_llm(*, messages, tools, model, timeout_s):
    """Forward to pm_agent._call_llm at call time so monkeypatching wins."""
    return _pm._call_llm(
        messages=messages, tools=tools, model=model, timeout_s=timeout_s,
    )

log = logging.getLogger(__name__)


@dataclass
class AgentAction:
    action: str
    target_id: int | None
    payload: dict
    rationale: str
    tool_result: dict


@dataclass
class AgentDecisionResult:
    success: bool
    actions_taken: list[AgentAction]
    narrative: str
    turns_used: int
    elapsed_s: float
    fallback_used: bool
    raw_messages: list[dict] = field(default_factory=list)
    error: str | None = None


def _record_llm_usage(*, db_path: str, tokens: int) -> None:
    """Append one call's token usage to llm_usage. Never raises."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_usage ("
            "ts_utc TEXT NOT NULL, tokens INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO llm_usage (ts_utc, tokens) VALUES "
            "(strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?)", (int(tokens),))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        log.exception("llm_usage.record_failed")


def daily_tokens_used(*, db_path: str) -> int:
    """Total recorded tokens since UTC midnight. 0 on any error."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=30000")
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens),0) FROM llm_usage "
            "WHERE ts_utc >= strftime('%Y-%m-%dT00:00:00Z','now')"
        ).fetchone()
        conn.close()
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0


# Teams that keep running even over budget — position protection beats pacing.
_BUDGET_EXEMPT_TEAMS = {"trader"}
_DEFAULT_DAILY_TOKEN_BUDGET = 300_000_000  # ~2.1B/wk quota ≈ 2.8B; leave headroom


def _sanitize_tool_call_json(msg: dict) -> dict:
    """Force every tool_call's `arguments` to be a VALID JSON string before the
    assistant message enters history.

    M3 intermittently emits malformed JSON in a tool call's arguments. Left in
    the message history, MiniMax rejects the NEXT request with a 400 ('invalid
    function arguments json string, tool_call_id: ...') and the whole cycle
    aborts. Re-serializing to canonical JSON (or '{}' if unparseable) keeps the
    history clean so the loop continues and M3 can self-correct next turn.
    """
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function")
        if not isinstance(fn, dict):
            continue
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            fn["arguments"] = json.dumps(raw)
            continue
        try:
            fn["arguments"] = json.dumps(json.loads(raw or "{}"))
        except (json.JSONDecodeError, TypeError):
            log.warning("agent_runner.repaired_bad_tool_json name=%s",
                        fn.get("name"))
            fn["arguments"] = "{}"
    return msg


def run_agent_loop(
    *,
    db_path: str,
    system_prompt: str,
    user_kickoff: str,
    tools: dict[str, Callable[..., Any]],
    schemas: list[dict],
    turn_budget: int,
    time_budget_s: int,
    minimax_model: str = "minimax/MiniMax-M3",
    team: str | None = None,
) -> AgentDecisionResult:
    """Run the standard ReAct-ish loop. Pure-deterministic when LLM is mocked."""
    started = time.time()

    if not os.environ.get("MINIMAX_API_KEY"):
        log.warning("agent_runner.no_api_key.fallback_noop")
        return AgentDecisionResult(
            success=True,
            actions_taken=[],
            narrative="M3 unreachable (MINIMAX_API_KEY missing); no actions taken.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    # Recover work wedged in 'claimed' by a dead/timed-out agent — every
    # specialist passes through here, so stale claims get reaped each cycle.
    try:
        from src.research.agent_tools import reap_stale_claims
        reap_stale_claims(db_path=db_path)
    except Exception:  # noqa: BLE001
        log.exception("agent_runner.reap_failed")

    # Daily token budget — pace the crew so it lives all week instead of
    # slamming into the MiniMax quota wall mid-week (2026-07-09: 100% burned,
    # every agent dead INCLUDING the trader during a live position emergency).
    # The trader is exempt: position protection beats pacing.
    budget = int(os.environ.get("JUDAS_DAILY_TOKEN_BUDGET", _DEFAULT_DAILY_TOKEN_BUDGET))
    if budget > 0 and (team or "") not in _BUDGET_EXEMPT_TEAMS:
        used = daily_tokens_used(db_path=db_path)
        if used >= budget:
            log.warning("agent_runner.daily_budget_reached team=%s used=%d budget=%d — skipping cycle",
                        team, used, budget)
            return AgentDecisionResult(
                success=True, actions_taken=[],
                narrative=f"daily token budget reached ({used:,}/{budget:,}) — cycle skipped",
                turns_used=0, elapsed_s=time.time() - started,
                fallback_used=True, raw_messages=[], error=None,
            )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_kickoff},
    ]
    actions: list[AgentAction] = []
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
        response = None
        quota_exhausted = False
        for _llm_try in range(3):
            try:
                response = _call_llm(
                    messages=messages,
                    tools=schemas,
                    model=minimax_model,
                    timeout_s=min(remaining, 300),
                )
                error = None
                break
            except Exception as exc:  # noqa: BLE001
                # Quota exhaustion (MiniMax 429 "Token Plan usage limit reached")
                # is NOT a crash — retrying just burns time, and exiting failed
                # leaves the systemd unit red masking real failures (2026-07-09:
                # the trader died mid-emergency this way). Skip cleanly; the
                # timer retries next cycle after the window resets.
                s = str(exc)
                if "rate_limit" in s or "usage limit" in s or '"429"' in s:
                    quota_exhausted = True
                    log.warning("agent_runner.quota_exhausted — skipping cycle cleanly")
                    break
                error = f"llm call failed: {exc}"
                log.warning("agent_runner.llm_retry attempt=%d err=%s", _llm_try + 1, exc)
                time.sleep(2)
        if quota_exhausted:
            return AgentDecisionResult(
                success=True, actions_taken=actions,
                narrative="MiniMax quota exhausted — cycle skipped, timer retries after reset.",
                turns_used=turn, elapsed_s=time.time() - started,
                fallback_used=True, raw_messages=messages, error=None,
            )
        if response is None:
            break  # all retries exhausted

        # Token accounting → llm_usage(day, tokens). The daily budget guard in
        # the runners reads this; without metering nothing stops a runaway from
        # hitting the MiniMax wall mid-week (2.33B burned in 7 days, 2026-07-09).
        try:
            u = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
            tok = int((u or {}).get("total_tokens", 0) if isinstance(u, dict) else getattr(u, "total_tokens", 0) or 0)
            if tok:
                _record_llm_usage(db_path=db_path, tokens=tok)
        except Exception:  # noqa: BLE001
            pass

        # Sanitize tool-call JSON BEFORE it enters history (M3 can emit malformed
        # arguments that 400 the next request and abort the cycle).
        msg = _sanitize_tool_call_json(_extract_message(response))
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
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": name,
                "content": json.dumps(result, default=str),
            })
            if agent_tools.is_action_tool(name):
                target_id = None
                for k in ("id", "task_id", "target_id"):
                    if isinstance(args.get(k), int):
                        target_id = args[k]
                        break
                actions.append(AgentAction(
                    action=name,
                    target_id=target_id,
                    payload=dict(args),
                    rationale=str(args.get("rationale") or args.get("reason")
                                  or args.get("notes") or ""),
                    tool_result=result if isinstance(result, dict)
                                else {"value": result},
                ))

    elapsed_s = time.time() - started
    if not final_text:
        last_assistant = next(
            (m.get("content") for m in reversed(messages)
             if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        if last_assistant:
            final_text = str(last_assistant).strip()
        else:
            final_text = (
                f"Cycle ended with {len(actions)} action(s); turns={turn}, "
                f"elapsed={elapsed_s:.1f}s."
            )

    return AgentDecisionResult(
        success=error is None,
        actions_taken=actions,
        narrative=final_text,
        turns_used=turn,
        elapsed_s=elapsed_s,
        fallback_used=False,
        raw_messages=messages,
        error=error,
    )


def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
