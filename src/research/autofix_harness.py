"""M3 auto-fix harness — Phase 3b of the Agentic Operator Plan.

A constrained tool-using agent that runs MiniMax M3 inside a git worktree to
diagnose and patch a focused symptom. The harness is intentionally small and
defensive: every tool validates paths inside ``worktree_path``, ``apply_patch``
enforces an allow/deny list against the diff, and budgets are enforced both on
turn count and wall-clock time.

The supervisor (``operator_flow.fix_bug_step``) wires git worktree creation,
push, and DB persistence around this module. This module returns a
``HarnessResult`` and never touches anything outside its worktree.

Contract: see ``PHASE3_DESIGN.md`` §"Autofix executor: MiniMax M3" and the
"Tools exposed to M3" deliverable in the Phase 3b worker brief.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_READ_FILE_CAP_BYTES = 32 * 1024
_GREP_RESULT_CAP = 100
_LIST_FILES_CAP = 200
_DIFF_TEXT_CAP_BYTES = 8 * 1024
_GIT_DIFF_CAP_BYTES = 16 * 1024
_TEST_OUTPUT_TAIL_BYTES = 4 * 1024
_PYTEST_TIMEOUT_S = 300

_SYSTEM_PROMPT_TEMPLATE = """\
You are an autonomous code-fix agent operating inside a git worktree of
the `judas-agentic-crew` repo. This is a real paper-trading system on
IBKR — every cycle it doesn't trade is real lost P&L. Your job is to
land an actual production fix, not a stub.

PROJECT MAP (start here, don't grep blind):
- main.py — top-level entry point. CLI flags route to runtimes; argparse
  defines --eval-only / --runtime etc. Look here for "place_orders=..."
  wiring.
- src/portfolio_runtime.py — the production fire→order conversion path
  (run_portfolio_scan, _route_one_fire). Where signals become orders.
  Any short-circuit like `skip_reason='eval_only_no_orders'` lives here.
- src/research/ — Operator, Trader, Researcher, Registrar, Coder
  agents, agent tools, brief, autofix harness.
- src/flows/operator_flow.py — CrewAI flow brain.
- src/strategies/ — strategy implementations (zoo, buffet, judas_*).
- src/strategy_registry.py — active_strategies table CRUD.
- src/db/models.py — SQLite schema + get_conn helper.
- src/dashboard/app.py — Flask dashboard API.
- src/tools/ibkr_executor.py — IBKR order placement (DENYLISTED — do
  not edit). All broker writes happen here.
- src/risk/** — sleeve cap + kill switch (DENYLISTED).
- config.yaml, src/config.py — runtime config (DENYLISTED).
- tests/ — pytest. ~177 tests. Must stay green.

CONSTRAINTS:
- You may modify any file EXCEPT files matching the denylist patterns.
- You have {turn_budget} tool calls and {time_budget_s} seconds.
- Goal: land a real production fix that makes pytest pass AND would
  unblock the described symptom in real execution.

TOOLS:
- read_file, list_files, grep — exploration
- apply_patch — make changes (unified diff format)
- run_tests — run pytest -q
- git_status, git_diff — see your work

OUTPUT:
When done, call run_tests one final time. If passed, signal completion
in your reply. If you cannot fix without touching denylist files,
explain why and stop.

DO NOT GAME THE HARNESS:
- A patch that only adds harmless meta files (e.g. `.autofix-denylist`,
  README scratch, dummy modules) without touching real source code is
  NOT a fix and will be rejected. You must change at least one .py
  file under `src/`, `main.py`, or `tests/` that materially addresses
  the symptom.
- Do NOT touch denylist files.
- Do NOT make speculative changes — minimal is correct, but it must
  be on the actual code path the symptom describes.

Allowlist: {allowlist}
Denylist:  {denylist}
"""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class HarnessResult:
    success: bool
    files_changed: list[str]
    diff_summary: str
    diff_text: str | None
    test_passed: bool
    test_output_tail: str
    turns_used: int
    elapsed_s: float
    error: str | None
    raw_messages: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _safe_resolve(worktree_path: str, rel_path: str) -> Path | None:
    """Return absolute Path inside ``worktree_path`` or ``None`` if traversal."""
    root = Path(worktree_path).resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _matches_any(path: str, patterns: list[str]) -> bool:
    norm = path.lstrip("./")
    for pat in patterns:
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(path, pat):
            return True
        # Also accept directory-prefix style (e.g. "src/research/**")
        if pat.endswith("/**") and norm.startswith(pat[:-3] + "/"):
            return True
        if pat.endswith("/**") and norm == pat[:-3]:
            return True
    return False


# ---------------------------------------------------------------------------
# Diff parsing — extract changed file paths from a unified diff
# ---------------------------------------------------------------------------


def _changed_files_from_diff(diff_text: str) -> list[str]:
    files: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            # strip "b/" prefix that git produces
            if target.startswith("b/"):
                target = target[2:]
            if target == "/dev/null":
                continue
            if target and target not in files:
                files.append(target)
        elif line.startswith("--- "):
            target = line[4:].strip()
            if target.startswith("a/"):
                target = target[2:]
            if target == "/dev/null":
                continue
            # Only add if not already present via +++ (covers pure delete)
            if target and target not in files:
                files.append(target)
    return files


def _diff_has_traversal(diff_text: str) -> bool:
    for f in _changed_files_from_diff(diff_text):
        if ".." in Path(f).parts or f.startswith("/"):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool implementations — closures bound to a worktree + lists
# ---------------------------------------------------------------------------


def _make_tools(
    *,
    worktree_path: str,
    allowlist: list[str],
    denylist: list[str],
) -> dict[str, Callable[..., Any]]:
    root = str(Path(worktree_path).resolve())

    def read_file(path: str) -> dict:
        resolved = _safe_resolve(root, path)
        if resolved is None:
            return {"error": "path traversal rejected", "content": "", "lines": 0}
        if not resolved.is_file():
            return {"error": "not a file", "content": "", "lines": 0}
        data = resolved.read_bytes()
        truncated = False
        if len(data) > _READ_FILE_CAP_BYTES:
            data = data[:_READ_FILE_CAP_BYTES]
            truncated = True
        text = data.decode("utf-8", errors="replace")
        return {
            "content": text,
            "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "truncated": truncated,
        }

    def list_files(path: str = ".", glob_pattern: str = "**/*") -> list[str]:
        base = _safe_resolve(root, path)
        if base is None or not base.exists():
            return []
        results: list[str] = []
        root_path = Path(root)
        for p in base.glob(glob_pattern):
            if p.is_file():
                try:
                    rel = p.resolve().relative_to(root_path)
                except ValueError:
                    continue
                results.append(str(rel))
                if len(results) >= _LIST_FILES_CAP:
                    break
        return results

    def grep(pattern: str, glob: str = "**/*.py") -> list[dict]:
        # Use python-side scan for portability and to enforce caps.
        root_path = Path(root)
        matches: list[dict] = []
        for p in root_path.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.resolve().relative_to(root_path)
            except ValueError:
                continue
            rel_str = str(rel)
            if not fnmatch.fnmatch(rel_str, glob):
                continue
            try:
                with p.open("r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if pattern in line:
                            matches.append(
                                {
                                    "file": rel_str,
                                    "line_no": i,
                                    "text": line.rstrip("\n")[:400],
                                }
                            )
                            if len(matches) >= _GREP_RESULT_CAP:
                                return matches
            except OSError:
                continue
        return matches

    def apply_patch(diff_text: str) -> dict:
        if not diff_text or not diff_text.strip():
            return {"ok": False, "files_changed": [], "error": "empty diff"}
        if _diff_has_traversal(diff_text):
            return {
                "ok": False,
                "files_changed": [],
                "error": "path traversal in diff rejected",
            }
        changed = _changed_files_from_diff(diff_text)
        if not changed:
            return {
                "ok": False,
                "files_changed": [],
                "error": "could not parse changed files from diff",
            }
        for f in changed:
            if _matches_any(f, denylist):
                return {
                    "ok": False,
                    "files_changed": [],
                    "error": f"denylist violation: {f}",
                }
            if not _matches_any(f, allowlist):
                return {
                    "ok": False,
                    "files_changed": [],
                    "error": f"allowlist violation: {f} not in allowlist",
                }
        # Apply via `git apply` — write to a temp file inside the worktree.
        patch_path = Path(root) / ".autofix_patch.diff"
        patch_path.write_text(diff_text, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch_path)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "files_changed": [], "error": "git apply timeout"}
        finally:
            try:
                patch_path.unlink()
            except OSError:
                pass
        if proc.returncode != 0:
            return {
                "ok": False,
                "files_changed": [],
                "error": f"git apply failed: {proc.stderr.strip()[:500]}",
            }
        return {"ok": True, "files_changed": changed, "error": None}

    def run_tests(args: list[str] | None = None) -> dict:
        # Invoke pytest via the parent process's interpreter so the
        # worktree shares the project's venv (with ib_async, etc.) — not
        # whatever `pytest` resolves to on the system PATH. The worktree
        # is a separate git checkout but the venv lives at the repo root
        # and is shared across worktrees.
        cmd = [sys.executable, "-m", "pytest", "-q"]
        if args:
            cmd.extend(args)
        try:
            proc = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_PYTEST_TIMEOUT_S,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            tail = output[-_TEST_OUTPUT_TAIL_BYTES:]
            return {"passed": proc.returncode == 0, "output_tail": tail, "rc": proc.returncode}
        except subprocess.TimeoutExpired as exc:
            tail = (exc.output or "")[-_TEST_OUTPUT_TAIL_BYTES:] if exc.output else "timeout"
            return {"passed": False, "output_tail": tail, "rc": -1}
        except FileNotFoundError:
            return {"passed": False, "output_tail": "pytest not found", "rc": -2}

    def git_status() -> dict:
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"clean": False, "files": [], "error": "timeout"}
        files = []
        for line in (proc.stdout or "").splitlines():
            if not line.strip():
                continue
            # porcelain: "XY filename"
            status = line[:2].strip() or "?"
            fname = line[3:]
            files.append({"file": fname, "status": status})
        return {"clean": len(files) == 0, "files": files}

    def git_diff(staged: bool = False) -> dict:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--staged")
        try:
            proc = subprocess.run(
                cmd, cwd=root, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            return {"diff": "<timeout>"}
        out = proc.stdout or ""
        if len(out) > _GIT_DIFF_CAP_BYTES:
            out = out[:_GIT_DIFF_CAP_BYTES] + "\n<truncated>"
        return {"diff": out}

    return {
        "read_file": read_file,
        "list_files": list_files,
        "grep": grep,
        "apply_patch": apply_patch,
        "run_tests": run_tests,
        "git_status": git_status,
        "git_diff": git_diff,
    }


# ---------------------------------------------------------------------------
# Tool schemas for litellm tool_calls
# ---------------------------------------------------------------------------


def _tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file inside the worktree. Capped at 32KB.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files inside the worktree (max 200).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "default": "."},
                        "glob_pattern": {"type": "string", "default": "**/*"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Search for a substring in files matching glob. Max 100 matches.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "glob": {"type": "string", "default": "**/*.py"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": (
                    "Apply a unified diff to the worktree. Every changed file "
                    "must be on the allowlist and not on the denylist."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"diff_text": {"type": "string"}},
                    "required": ["diff_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run pytest -q in the worktree. 5-min timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "args": {"type": "array", "items": {"type": "string"}}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "Show git status of the worktree.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "Show git diff of the worktree (capped 16KB).",
                "parameters": {
                    "type": "object",
                    "properties": {"staged": {"type": "boolean", "default": False}},
                },
            },
        },
    ]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _call_llm(
    *,
    messages: list[dict],
    tools: list[dict],
    model: str,
    timeout_s: int,
) -> Any:
    """Wrap litellm so tests can monkeypatch this single seam."""
    import litellm  # type: ignore[import-not-found]

    return litellm.completion(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        timeout=timeout_s,
        temperature=0.0,
    )


def _extract_message(response: Any) -> dict:
    """Normalize litellm response to a plain dict message."""
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


def run_harness(
    *,
    prompt: str,
    worktree_path: str,
    allowlist: list[str],
    denylist: list[str],
    turn_budget: int = 0,
    time_budget_s: int = 0,
    pytest_args: list[str] | None = None,
    minimax_model: str = "minimax/MiniMax-M3",
) -> HarnessResult:
    """Run M3 against a git worktree to fix a focused symptom.

    See module docstring + PHASE3_DESIGN.md §"Autofix executor" for design.
    """
    start = time.time()

    if not os.environ.get("MINIMAX_API_KEY"):
        return HarnessResult(
            success=False,
            files_changed=[],
            diff_summary="",
            diff_text=None,
            test_passed=False,
            test_output_tail="",
            turns_used=0,
            elapsed_s=time.time() - start,
            error="MINIMAX_API_KEY not set",
            raw_messages=[],
        )

    if not Path(worktree_path).is_dir():
        return HarnessResult(
            success=False,
            files_changed=[],
            diff_summary="",
            diff_text=None,
            test_passed=False,
            test_output_tail="",
            turns_used=0,
            elapsed_s=time.time() - start,
            error=f"worktree_path does not exist: {worktree_path}",
            raw_messages=[],
        )

    tools = _make_tools(
        worktree_path=worktree_path, allowlist=allowlist, denylist=denylist
    )
    schemas = _tool_schemas()

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        turn_budget=turn_budget,
        time_budget_s=time_budget_s,
        allowlist=", ".join(allowlist),
        denylist=", ".join(denylist),
    )
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    turn = 0
    error: str | None = None
    unlimited_turns = turn_budget is None or turn_budget <= 0
    unlimited_time = time_budget_s is None or time_budget_s <= 0
    while unlimited_turns or turn < turn_budget:
        elapsed = time.time() - start
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
        except Exception as exc:  # noqa: BLE001 — defensive on the LLM seam
            error = f"llm call failed: {exc}"
            break

        msg = _extract_message(response)
        # Append assistant message verbatim so the model sees its own context.
        messages.append(msg)
        turn += 1

        tool_calls = _tool_calls_from(msg)
        if not tool_calls:
            # Model finished talking — stop.
            break

        for tc in tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name")
            arg_str = fn.get("arguments") or "{}"
            try:
                args = json.loads(arg_str) if isinstance(arg_str, str) else dict(arg_str)
            except json.JSONDecodeError:
                args = {}
            tool_fn = tools.get(name)
            if tool_fn is None:
                result: Any = {"error": f"unknown tool {name!r}"}
            else:
                try:
                    result = tool_fn(**args)
                except TypeError as exc:
                    result = {"error": f"bad args: {exc}"}
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"tool failed: {exc}"}
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name or "",
                    "content": json.dumps(result, default=str),
                }
            )

    elapsed_s = time.time() - start

    # After the loop, observe final state of the worktree.
    files_changed: list[str] = []
    diff_summary = ""
    diff_text: str | None = None
    test_passed = False
    test_output_tail = ""

    try:
        st = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in (st.stdout or "").splitlines():
            if line.strip():
                files_changed.append(line[3:])

        ds = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff_summary = (ds.stdout or "").strip()

        full_diff = subprocess.run(
            ["git", "diff"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = full_diff.stdout or ""
        if out:
            if len(out) > _DIFF_TEXT_CAP_BYTES:
                diff_text = out[:_DIFF_TEXT_CAP_BYTES] + "\n<truncated>"
            else:
                diff_text = out
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        error = error or f"git introspection failed: {exc}"

    if files_changed and not error:
        test_result = tools["run_tests"](pytest_args)
        test_passed = bool(test_result.get("passed"))
        test_output_tail = test_result.get("output_tail", "")
    elif error is None and not files_changed:
        error = "no patch produced"

    success = bool(files_changed) and test_passed and error is None

    if not success and error is None:
        if not test_passed and files_changed:
            error = "pytest failed"

    return HarnessResult(
        success=success,
        files_changed=files_changed,
        diff_summary=diff_summary,
        diff_text=diff_text,
        test_passed=test_passed,
        test_output_tail=test_output_tail,
        turns_used=turn,
        elapsed_s=elapsed_s,
        error=error,
        raw_messages=messages,
    )
