"""Independent autofix reviewer — the second check before a fix merges (2026-06-25).

The deterministic gate is testable without an API key (it returns before the LLM
call). It must reject the failure mode that motivated it: autofix #352 passed
pytest but only edited `.autofix-denylist` — a no-op that fixed nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.research.autofix_dispatch import _review_patch_is_real


def _review(files):
    return _review_patch_is_real(
        worktree_path="/tmp", symptom_category="tool_failure",
        symptom_summary="custom engine silent", files_changed=files,
    )


def test_meta_only_diff_rejected_like_352():
    # The exact #352 case: only .autofix-denylist changed.
    r = _review([".autofix-denylist"])
    assert r["real"] is False
    assert "meta-only" in r["reason"]


def test_docs_and_text_only_rejected():
    assert _review(["docs/README.md", "notes.txt", "CHANGELOG"])["real"] is False


def test_real_source_change_passes_deterministic_gate():
    # A diff that touches real source/test files passes the deterministic gate
    # (then the LLM gate would run — not exercised here without an API key).
    # We assert it is NOT rejected by the meta-only rule.
    r = _review_patch_is_real(
        worktree_path="/tmp", symptom_category="x", symptom_summary="y",
        files_changed=["src/portfolio_runtime.py"],
    )
    # With no git worktree at /tmp, the diff step fails open → real=True.
    assert r["real"] is True
