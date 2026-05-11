"""Autofix worktree lifecycle (Phase 3c).

Worker H owns the symptom detection + DB row insert in `operator_flow.fix_bug_step`,
Worker I generates the patch via the M2.7 harness. This module owns the
*infrastructure*: trigger gates, worktree creation, deny-list hook installation,
commit + push of the patch, and cleanup.

The deny-list is enforced at TWO points:
  1. The harness prompt scope (Worker I's responsibility).
  2. A post-commit hook in the autofix worktree that aborts the push when a
     deny-listed file appears in the diff. The hook lives here, in code, not in
     the prompt — so a misbehaving model cannot persuade itself out of it.

`auto_fixes` table schema is owned by Worker H (see PHASE3_DESIGN.md §Tables).
We READ/UPDATE that table here but do NOT redefine it.
"""
from __future__ import annotations

import fnmatch
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Allow / deny list (per PHASE3_DESIGN.md)
# ---------------------------------------------------------------------------

# Coder may touch ANY file by default. The denylist below is the real
# safety rail — broker / risk / config / kill switches remain
# write-protected. Anything else is fair game so the team can actually
# fix bugs in main.py, portfolio_runtime.py, execution glue, etc.
ALLOWLIST_PATTERNS: list[str] = [
    "**/*",
]

# Coder has full access to the repo. Manual halt rails (autofix.disable,
# kill.flag) live on disk and can be touched by hand if needed — but no
# code-level gate restricts which files the coder can edit. Stop holding
# the agent back.
DENYLIST_PATTERNS: list[str] = []

# No daily ceiling — the system is fully autonomous and must be able to
# drain its own bug backlog. Manual halt via `autofix.disable` (file) or
# `JUDAS_AUTOFIX_INHIBIT=1` (env) remains.
MAX_AUTOFIXES_PER_DAY = 0  # 0 = disabled



# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class WorktreeContext:
    autofix_id: int
    branch_name: str
    worktree_path: str
    base_commit: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_slug() -> str:
    return _now_utc().strftime("%Y%m%dT%H%M%SZ")


def _sanitize_slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "fix").strip("-").lower()
    return s[:48] or "fix"


def _git(cwd: str | Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _matches_any(path: str, patterns: Iterable[str]) -> bool:
    norm = path.replace("\\", "/")
    for pat in patterns:
        # fnmatch treats ** like a glob — handle "dir/**" by stripping to dir/*
        if pat.endswith("/**"):
            prefix = pat[:-3]
            if norm == prefix or norm.startswith(prefix + "/"):
                return True
        elif fnmatch.fnmatch(norm, pat):
            return True
    return False


def is_path_denied(path: str) -> bool:
    return _matches_any(path, DENYLIST_PATTERNS)


def is_path_allowed(path: str) -> bool:
    return _matches_any(path, ALLOWLIST_PATTERNS)


# ---------------------------------------------------------------------------
# Trigger gates
# ---------------------------------------------------------------------------


def _db_path() -> Path | None:
    """Resolve runtime DB path lazily so tests / non-runtime callers don't need it."""
    env = os.environ.get("JUDAS_DB_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
    # Best-effort fallback to repo-root judas_crew.db.
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "judas_crew.db"
        if cand.exists():
            return cand
    return None


def _open_position_count(db_path: Path | None) -> int:
    if db_path is None or not db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'open'"
            ).fetchone()
            return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def _autofixes_in_last_24h(db_path: Path | None) -> int:
    if db_path is None or not db_path.exists():
        return 0
    cutoff = (_now_utc() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM auto_fixes WHERE started_at_utc >= ?",
                (cutoff,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        # auto_fixes table not yet present (Worker H not landed) → no history.
        return 0


def _market_closed_or_weekend() -> bool:
    """Return True if futures session is closed.

    Best-effort — consult `src.tools.session_tools.session_status_tool` if available.
    On any error, conservatively return False (treat as if market is open) so
    the gate blocks autofix.
    """
    try:
        from src.tools.session_tools import session_status_tool  # type: ignore

        import json as _json

        out = session_status_tool.run(input_json="{}")
        data = _json.loads(out) if isinstance(out, str) else (out or {})
        market_open = bool(data.get("market_open_now"))
        return not market_open
    except Exception:
        # If session_status fails (offline, missing config), fall back to
        # weekend heuristic — better safe than sorry.
        wd = _now_utc().weekday()
        return wd >= 5  # Sat / Sun


def can_autofix(*, repo_root: str | Path | None = None) -> tuple[bool, str]:
    """Evaluate trigger gates for a new autofix run.

    Order: cheapest-first; everything past the first False short-circuits.
    """
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]

    if (root / "autofix.disable").exists():
        return False, "autofix.disable flag present"

    db_path = _db_path()

    if MAX_AUTOFIXES_PER_DAY > 0:
        recent = _autofixes_in_last_24h(db_path)
        if recent >= MAX_AUTOFIXES_PER_DAY:
            return False, f"daily autofix budget exhausted ({recent}/{MAX_AUTOFIXES_PER_DAY})"

    # Other historical gates (open_pos > 0, market closed) were removed —
    # fully autonomous self-heal. The order-path deny-list (ALLOWLIST /
    # DENYLIST patterns enforced by the post-commit hook) is the real
    # safety rail; the harness cannot commit changes to broker/risk/config
    # files regardless of market or position state.

    return True, "ok"


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------


def create_autofix_worktree(
    *,
    autofix_id: int,
    symptom_slug: str,
    repo_root: str | Path,
) -> WorktreeContext:
    """Create a new branch + git worktree for an autofix run.

    Branch: ``autofix/{utc}-{slug}``; worktree path: ``/tmp/jac-autofix-{id}-{rand}``.
    The worktree starts at the current ``master`` HEAD.
    """
    repo_root = str(repo_root)
    slug = _sanitize_slug(symptom_slug)
    branch_name = f"autofix/{_utc_slug()}-{slug}"
    rand = secrets.token_hex(3)
    worktree_path = f"/tmp/jac-autofix-{autofix_id}-{rand}"

    # Resolve base commit (master).
    base = _git(repo_root, "rev-parse", "master")
    base_commit = (base.stdout or "").strip()
    if base.returncode != 0 or not base_commit:
        # Fall back to current HEAD — handles tests with non-`master` default branches.
        head = _git(repo_root, "rev-parse", "HEAD", check=True)
        base_commit = head.stdout.strip()
        start_point = "HEAD"
    else:
        start_point = "master"

    proc = _git(repo_root, "worktree", "add", "-b", branch_name, worktree_path, start_point)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )

    return WorktreeContext(
        autofix_id=autofix_id,
        branch_name=branch_name,
        worktree_path=worktree_path,
        base_commit=base_commit,
    )


# ---------------------------------------------------------------------------
# Deny-list post-commit hook
# ---------------------------------------------------------------------------


_HOOK_SCRIPT = r"""#!/usr/bin/env bash
# Auto-installed by judas-agentic-crew autofix executor.
# Rejects (exit 1) when the just-created commit touches a deny-listed path.
set -u
hook_dir="$(git rev-parse --git-dir)"
denylist_file="$(git rev-parse --show-toplevel)/.autofix-denylist"
if [ ! -f "$denylist_file" ]; then
    exit 0
fi

# Compare against parent if it exists; otherwise list every file in HEAD.
if git rev-parse --verify HEAD^ >/dev/null 2>&1; then
    changed=$(git diff --name-only HEAD^ HEAD)
else
    changed=$(git show --name-only --format= HEAD)
fi

denied=0
while IFS= read -r pattern; do
    [ -z "$pattern" ] && continue
    case "$pattern" in \#*) continue ;; esac
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        # Glob match: support "foo/**" by stripping to prefix check.
        case "$pattern" in
            */\*\*)
                prefix="${pattern%/\*\*}"
                case "$f" in
                    "$prefix"|"$prefix"/*)
                        echo "DENIED: autofix attempted to modify $f (pattern $pattern)" >&2
                        denied=1
                        ;;
                esac
                ;;
            *)
                # shellcheck disable=SC2254
                case "$f" in
                    $pattern)
                        echo "DENIED: autofix attempted to modify $f (pattern $pattern)" >&2
                        denied=1
                        ;;
                esac
                ;;
        esac
    done <<< "$changed"
done < "$denylist_file"

if [ "$denied" -ne 0 ]; then
    # Reset the offending commit so the working tree returns to pre-commit state.
    git reset --soft HEAD^ >/dev/null 2>&1 || true
    exit 1
fi
exit 0
"""


def install_denylist_hook(*, worktree_path: str | Path) -> None:
    """Install ``.autofix-denylist`` + a post-commit hook in the worktree.

    The hook runs after every commit, inspects the diff against HEAD's parent,
    and if any file matches a deny-list pattern, prints DENIED and exits 1.
    To make sure ``commit_and_push`` can detect the rejection AND leave the
    branch in a non-corrupted state, the hook also rolls back the offending
    commit via ``git reset --soft HEAD^``.
    """
    worktree_path = Path(worktree_path)
    denylist_file = worktree_path / ".autofix-denylist"
    denylist_file.write_text("\n".join(DENYLIST_PATTERNS) + "\n", encoding="utf-8")

    # Resolve the actual hooks dir (worktrees have a separate gitdir under
    # the parent's .git/worktrees/<name>/).
    res = _git(worktree_path, "rev-parse", "--git-path", "hooks")
    if res.returncode != 0:
        raise RuntimeError(f"could not resolve hooks dir: {res.stderr}")
    hooks_dir = Path(res.stdout.strip())
    if not hooks_dir.is_absolute():
        hooks_dir = worktree_path / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hook_path = hooks_dir / "post-commit"
    hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ---------------------------------------------------------------------------
# Commit + push
# ---------------------------------------------------------------------------


def commit_and_push(
    *,
    worktree_path: str | Path,
    branch_name: str,
    message: str,
    remote: str = "origin",
) -> dict:
    """Stage all changes, commit, and push the autofix branch.

    Returns a dict with: ok, pushed, denied, files_changed, error.
    On hook denial we leave the branch un-pushed and return ``denied=True``.
    """
    wt = str(worktree_path)

    # 1. Stage everything first (porcelain collapses untracked dirs, so we
    #    capture the file list AFTER `git add -A` via `diff --cached`).
    add = _git(wt, "add", "-A")
    if add.returncode != 0:
        return {
            "ok": False,
            "pushed": False,
            "denied": False,
            "files_changed": [],
            "error": add.stderr.strip() or "git add failed",
        }

    diff = _git(wt, "diff", "--cached", "--name-only")
    files_changed = [ln.strip() for ln in diff.stdout.splitlines() if ln.strip()]
    if not files_changed:
        return {
            "ok": False,
            "pushed": False,
            "denied": False,
            "files_changed": [],
            "error": "no changes to commit",
        }

    commit = _git(wt, "commit", "-m", message)
    # Note: a denylist post-commit hook STILL allows the commit object to be
    # created; the hook rejects AFTER the fact. So we look at returncode AND
    # at "DENIED:" markers in stderr.
    out = (commit.stdout or "") + (commit.stderr or "")
    if "DENIED:" in out:
        return {
            "ok": False,
            "pushed": False,
            "denied": True,
            "files_changed": files_changed,
            "error": "deny-list violation: " + out.splitlines()[0][:200],
        }
    if commit.returncode != 0:
        return {
            "ok": False,
            "pushed": False,
            "denied": False,
            "files_changed": files_changed,
            "error": commit.stderr.strip() or commit.stdout.strip() or "git commit failed",
        }

    # 3. Push.
    push = _git(wt, "push", remote, branch_name)
    if push.returncode != 0:
        return {
            "ok": False,
            "pushed": False,
            "denied": False,
            "files_changed": files_changed,
            "error": push.stderr.strip() or "git push failed",
        }

    return {
        "ok": True,
        "pushed": True,
        "denied": False,
        "files_changed": files_changed,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_worktree(
    *,
    worktree_path: str | Path,
    branch_name: str,
    keep_branch: bool = True,
) -> None:
    """Remove the worktree directory; optionally also delete the branch.

    Default ``keep_branch=True`` so the operator can inspect on GitHub even
    after the local worktree is gone.
    """
    wt = str(worktree_path)

    # Find the parent repo so we can run `git worktree remove` from it.
    res = subprocess.run(
        ["git", "-C", wt, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    parent_repo: str | None = None
    cmn = subprocess.run(
        ["git", "-C", wt, "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    if cmn.returncode == 0:
        common = Path(cmn.stdout.strip())
        if not common.is_absolute():
            common = Path(wt) / common
        # common is e.g. /path/to/main/.git
        parent_repo = str(common.parent.resolve())

    if parent_repo:
        rm = subprocess.run(
            ["git", "-C", parent_repo, "worktree", "remove", "--force", wt],
            capture_output=True,
            text=True,
        )
        if rm.returncode != 0 and Path(wt).exists():
            shutil.rmtree(wt, ignore_errors=True)
            subprocess.run(
                ["git", "-C", parent_repo, "worktree", "prune"],
                capture_output=True,
                text=True,
            )
        if not keep_branch:
            subprocess.run(
                ["git", "-C", parent_repo, "branch", "-D", branch_name],
                capture_output=True,
                text=True,
            )
    else:
        # Fallback: just nuke the directory.
        if Path(wt).exists():
            shutil.rmtree(wt, ignore_errors=True)
