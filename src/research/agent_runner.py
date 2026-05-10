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


def run_agent_loop(
    *,
    db_path: str,
    system_prompt: str,
    user_kickoff: str,
    tools: dict[str, Callable[..., Any]],
    schemas: list[dict],
    turn_budget: int,
    time_budget_s: int,
    minimax_model: str = "minimax/MiniMax-M2.7",
) -> AgentDecisionResult:
    """Run the standard ReAct-ish loop. Pure-deterministic when LLM is mocked."""
    started = time.time()

    if not os.environ.get("MINIMAX_API_KEY"):
        log.warning("agent_runner.no_api_key.fallback_noop")
        return AgentDecisionResult(
            success=True,
            actions_taken=[],
            narrative="M2.7 unreachable (MINIMAX_API_KEY missing); no actions taken.",
            turns_used=0,
            elapsed_s=time.time() - started,
            fallback_used=True,
            raw_messages=[],
            error=None,
        )

    # Second system message: TIME & MARKET STATE banner. Every agent gets
    # this so it can't miss market hours / bar-close timing when reasoning.
    try:
        from src.research.market_clock import format_market_banner
        time_banner = format_market_banner()
    except Exception:  # noqa: BLE001
        time_banner = ""

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
    ]
    if time_banner:
        messages.append({"role": "system", "content": time_banner})
    messages.append({"role": "user", "content": user_kickoff})
    actions: list[AgentAction] = []
    turn = 0
    error: str | None = None
    final_text = ""

    while turn < turn_budget:
        elapsed = time.time() - started
        if elapsed >= time_budget_s:
            error = f"time budget exhausted after {elapsed:.1f}s"
            break
        remaining = max(1, int(time_budget_s - elapsed))
        try:
            response = _call_llm(
                messages=messages,
                tools=schemas,
                model=minimax_model,
                timeout_s=min(remaining, 300),
            )
        except Exception as exc:  # noqa: BLE001
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
