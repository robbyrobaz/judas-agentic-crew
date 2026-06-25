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


def test_real_source_not_rejected_as_meta_only():
    # A diff that touches a real source file must NOT be rejected by the
    # deterministic meta-only rule (it proceeds to the diff/LLM gate, which is
    # not exercised here). Assert specifically that the meta-only reason did NOT
    # fire — the final verdict beyond that depends on the diff/LLM step.
    r = _review_patch_is_real(
        worktree_path="/tmp", symptom_category="x", symptom_summary="y",
        files_changed=["src/portfolio_runtime.py"],
    )
    assert "meta-only" not in r["reason"]


def test_empty_files_changed_not_meta_rejected():
    # No file list at all shouldn't trigger the meta-only rule (the diff/LLM
    # step handles emptiness); just confirm the meta gate doesn't false-fire.
    r = _review_patch_is_real(
        worktree_path="/tmp", symptom_category="x", symptom_summary="y",
        files_changed=[],
    )
    assert "meta-only" not in r["reason"]
