"""Tests for ``src.research.autofix_harness``.

Mocks the litellm completion call. Exercises the tool palette, path-safety
guards, allow/deny enforcement, and budget termination.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from src.research import autofix_harness as ah


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> None:
    """Initialise a git repo at ``path`` with one committed file."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "harness@test.local"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "harness-test"], cwd=path, check=True
    )
    (path / "src").mkdir()
    (path / "src" / "__init__.py").write_text("", encoding="utf-8")
    (path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (path / "src" / "secret.py").write_text("API_KEY = 'denied'\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (path / "tests" / "test_smoke.py").write_text(
        "from src.module import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    (path / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=path, check=True
    )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    return repo


@pytest.fixture
def api_key():
    """Set MINIMAX_API_KEY for the duration of the test."""
    with mock.patch.dict(os.environ, {"MINIMAX_API_KEY": "test-key"}):
        yield


def _scripted_response(tool_name: str | None, arguments: dict | None, *, content: str = "") -> dict:
    """Build a fake litellm completion response object as a dict."""
    if tool_name is None:
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content or "done",
                        "tool_calls": [],
                    }
                }
            ]
        }
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [
                        {
                            "id": f"call_{tool_name}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(arguments or {}),
                            },
                        }
                    ],
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# 1. Simple successful fix
# ---------------------------------------------------------------------------


def test_harness_simple_fix(worktree: Path, api_key) -> None:
    # Patch that adds a new function to the allowed module.
    diff = (
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1,4 @@\n"
        " VALUE = 1\n"
        "+\n"
        "+def added():\n"
        "+    return 42\n"
    )

    responses = iter(
        [
            _scripted_response("read_file", {"path": "src/module.py"}),
            _scripted_response("apply_patch", {"diff_text": diff}),
            _scripted_response(None, None, content="done"),
        ]
    )

    with mock.patch.object(ah, "_call_llm", side_effect=lambda **_: next(responses)):
        # Stub run_tests to avoid invoking real pytest.
        with mock.patch.object(
            ah,
            "_make_tools",
            wraps=ah._make_tools,
        ) as wrapper:
            real_result = ah.run_harness(
                prompt="add a function `added()`",
                worktree_path=str(worktree),
                allowlist=["src/module.py", "src/research/**"],
                denylist=["src/secret.py"],
                turn_budget=5,
                time_budget_s=60,
            )
            wrapper.assert_called_once()

    # Real pytest will run via the harness end-of-loop block. Assert at least
    # that the patch landed and run_tests was invoked.
    assert "src/module.py" in [f for f in real_result.files_changed]
    # run_tests should have been called; with no tests touching the new fn it passes.
    assert real_result.test_passed is True
    assert real_result.success is True
    assert real_result.error is None


# ---------------------------------------------------------------------------
# 2. Denylist violation
# ---------------------------------------------------------------------------


def test_harness_denylist_violation(worktree: Path, api_key) -> None:
    bad_diff = (
        "--- a/src/secret.py\n"
        "+++ b/src/secret.py\n"
        "@@ -1 +1 @@\n"
        "-API_KEY = 'denied'\n"
        "+API_KEY = 'pwned'\n"
    )

    responses = iter(
        [
            _scripted_response("apply_patch", {"diff_text": bad_diff}),
            _scripted_response(None, None, content="cannot fix without denylist"),
        ]
    )

    with mock.patch.object(ah, "_call_llm", side_effect=lambda **_: next(responses)):
        result = ah.run_harness(
            prompt="modify secret",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=["src/secret.py"],
            turn_budget=5,
            time_budget_s=60,
        )

    assert result.success is False
    assert result.files_changed == []
    # The tool result message should record the denylist error.
    tool_msgs = [m for m in result.raw_messages if m.get("role") == "tool"]
    assert any("denylist" in (m.get("content") or "") for m in tool_msgs)


# ---------------------------------------------------------------------------
# 3. Allowlist violation
# ---------------------------------------------------------------------------


def test_harness_allowlist_violation(worktree: Path, api_key) -> None:
    # File exists but is not on the allowlist.
    (worktree / "src" / "other.py").write_text("X = 0\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add other"], cwd=worktree, check=True)

    diff = (
        "--- a/src/other.py\n"
        "+++ b/src/other.py\n"
        "@@ -1 +1 @@\n"
        "-X = 0\n"
        "+X = 1\n"
    )
    responses = iter(
        [
            _scripted_response("apply_patch", {"diff_text": diff}),
            _scripted_response(None, None, content="stop"),
        ]
    )
    with mock.patch.object(ah, "_call_llm", side_effect=lambda **_: next(responses)):
        result = ah.run_harness(
            prompt="patch",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=["src/secret.py"],
            turn_budget=3,
            time_budget_s=30,
        )

    assert result.success is False
    tool_msgs = [m for m in result.raw_messages if m.get("role") == "tool"]
    assert any("allowlist" in (m.get("content") or "") for m in tool_msgs)


# ---------------------------------------------------------------------------
# 4. Pytest fails after a syntactically valid patch
# ---------------------------------------------------------------------------


def test_harness_pytest_fails(worktree: Path, api_key) -> None:
    # Patch breaks the test by changing VALUE.
    diff = (
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1 @@\n"
        "-VALUE = 1\n"
        "+VALUE = 999\n"
    )
    responses = iter(
        [
            _scripted_response("apply_patch", {"diff_text": diff}),
            _scripted_response(None, None, content="done"),
        ]
    )
    with mock.patch.object(ah, "_call_llm", side_effect=lambda **_: next(responses)):
        result = ah.run_harness(
            prompt="break it",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=[],
            turn_budget=3,
            time_budget_s=60,
        )

    assert result.test_passed is False
    assert result.success is False
    assert result.files_changed  # patch applied
    assert result.error == "pytest failed"


# ---------------------------------------------------------------------------
# 5. Turn budget
# ---------------------------------------------------------------------------


def test_harness_turn_budget(worktree: Path, api_key) -> None:
    """Model loops calling read_file forever; harness must terminate at budget."""

    def looper(**_kwargs):
        return _scripted_response("read_file", {"path": "src/module.py"})

    with mock.patch.object(ah, "_call_llm", side_effect=looper):
        result = ah.run_harness(
            prompt="loop",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=[],
            turn_budget=2,
            time_budget_s=60,
        )

    assert result.turns_used == 2
    assert result.success is False


# ---------------------------------------------------------------------------
# 6. Time budget
# ---------------------------------------------------------------------------


def test_harness_time_budget(worktree: Path, api_key) -> None:
    def slow(**_kwargs):
        time.sleep(0.2)
        return _scripted_response("read_file", {"path": "src/module.py"})

    with mock.patch.object(ah, "_call_llm", side_effect=slow):
        result = ah.run_harness(
            prompt="slow",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=[],
            turn_budget=100,
            time_budget_s=0.1,
        )

    assert result.success is False
    assert result.error and "time budget" in result.error
    assert result.turns_used >= 1  # one call sneaks through before re-check


# ---------------------------------------------------------------------------
# 7. No API key
# ---------------------------------------------------------------------------


def test_harness_no_api_key(worktree: Path) -> None:
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MINIMAX_API_KEY", None)
        result = ah.run_harness(
            prompt="x",
            worktree_path=str(worktree),
            allowlist=["src/module.py"],
            denylist=[],
        )
    assert result.success is False
    assert result.error == "MINIMAX_API_KEY not set"
    assert result.turns_used == 0


# ---------------------------------------------------------------------------
# 8. apply_patch path traversal in diff
# ---------------------------------------------------------------------------


def test_apply_patch_path_traversal(worktree: Path) -> None:
    tools = ah._make_tools(
        worktree_path=str(worktree),
        allowlist=["**/*"],
        denylist=[],
    )
    bad = (
        "--- a/../../etc/passwd\n"
        "+++ b/../../etc/passwd\n"
        "@@ -1 +1 @@\n"
        "-x\n+y\n"
    )
    out = tools["apply_patch"](bad)
    assert out["ok"] is False
    assert "traversal" in (out["error"] or "")


# ---------------------------------------------------------------------------
# 9. read_file path traversal
# ---------------------------------------------------------------------------


def test_read_file_path_traversal(worktree: Path) -> None:
    tools = ah._make_tools(
        worktree_path=str(worktree),
        allowlist=["**/*"],
        denylist=[],
    )
    out = tools["read_file"]("../../etc/passwd")
    assert out.get("error") == "path traversal rejected"
    assert out["content"] == ""


# ---------------------------------------------------------------------------
# 10. grep caps results
# ---------------------------------------------------------------------------


def test_grep_caps_results(worktree: Path) -> None:
    # Cap raised 2026-06-23 so the coder sees full grep sweeps. Verify it returns
    # everything below the (now generous) cap, and caps at _GREP_RESULT_CAP above it.
    cap = ah._GREP_RESULT_CAP
    (worktree / "src" / "small.py").write_text("\n".join(["needle here"] * 1000) + "\n")
    tools = ah._make_tools(worktree_path=str(worktree), allowlist=["**/*"], denylist=[])
    assert len(tools["grep"]("needle", "**/*.py")) == 1000  # all returned, under cap

    (worktree / "src" / "big.py").write_text("\n".join(["needle here"] * (cap + 50)) + "\n")
    assert len(tools["grep"]("needle", "**/*.py")) == cap  # capped above the limit
