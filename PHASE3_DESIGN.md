# Phase 3 — Code-fix Delegation Design

**Status:** draft for advisor review, 2026-05-09. **Do not implement until advisor signs off.**

This is the single highest-blast-radius phase in the operator plan. An LLM that auto-fixes bugs in code the running system depends on can either save you hours of operator time or quietly ship a regression to next Monday's open. The design below is conservative on purpose.

---

## What this is and isn't

**Is:** a `fix_bug_step` in `src/flows/operator_flow.py` that, when triggered, spawns a coding agent in an isolated git worktree with a focused prompt and a hard write-allowlist. The agent diagnoses, patches, runs tests, commits to a non-master branch, and pushes that branch to GitHub. A row lands in `auto_fixes` and a notification surfaces on the dashboard. The operator merges (or rejects) via the dashboard.

**Is NOT:**
- A hands-off committer to `master`. Every fix lands on `autofix/<...>` and waits for human merge.
- Allowed to touch order-routing code. Deny-list is enforced in code, not in prompt.
- Allowed to run with open positions. Trigger gate blocks during market hours.

---

## Trigger gates (ALL must be true)

```python
def can_autofix() -> tuple[bool, str]:
    if Path(REPO_ROOT / "autofix.disable").exists():
        return False, "autofix.disable flag present"
    if not _market_closed_or_weekend():
        return False, "market open"
    if _open_position_count() > 0:
        return False, f"{n} open positions"
    if _autofix_branch_exists_for(symptom_hash):
        return False, "autofix already in progress"
    if _autofixes_in_last_24h() >= MAX_AUTOFIXES_PER_DAY:
        return False, "daily autofix budget exhausted"
    return True, "ok"
```

`MAX_AUTOFIXES_PER_DAY = 3` (config). Prevents runaway loops where each autofix introduces a new symptom that triggers the next.

---

## Symptoms that trigger

The flow detects these in `morning_review` BEFORE classify routes:

1. **Repeated tool failure** — same exception in `research_experiments.errors` 3 times in 24h.
2. **Failing pytest** — nightly `pytest -q` non-zero exit.
3. **Looping research** — `runtime_status.json` flips to `state: timed_out` 3 days in a row.
4. **Naked-position drift** — Phase 0 reconcile flags any IBKR position without our DB row.
5. **Silent dry-run** — `would_*` log lines from `broker.*` when `dry_run_only` is False.

Each symptom has a `symptom_hash = sha1(category + first 200 chars of normalized stack/log)` for dedup.

---

## Autofix executor: MiniMax M2.7

Per the plan: M2.7 inside a thin custom harness using `litellm` directly. Reasons:

- M2.7 SWE-Pro 56% / Terminal Bench 57% / tool-call 75.8% — capable enough.
- ~2% of Opus cost — runs hot affordably.
- Already in the stack as the runtime reasoning model. No extra dependency.
- Hermes Agent considered but its v0.8 maturity + their own "use Claude Code for serious engineering" guidance argue against.
- Claude Code / Codex CLI / Agent Teams reserved as **escalation** (when M2.7 fails twice on the same symptom).

The harness is roughly `tools-v0`:
- Tools exposed to M2.7: `read_file`, `list_files`, `grep`, `apply_patch` (unified diff), `run_tests`, `git_status`, `git_diff`. No `bash`, no `write_file`. Patches go through the deny-list checker.
- Conversation budget: 30 turns or 30 minutes wall clock, whichever first.
- Single-shot prompt format: symptom description + relevant log lines + scope contract (allowed paths, denied paths, success criteria).

---

## Write-allowlist + deny-list (enforced in code, not prompt)

**Deny-list** (the agent cannot patch these — enforced by a post-commit hook in the autofix worktree):

```
src/tools/ibkr_executor.py
src/tools/ibkr_data.py
src/config.py
config.yaml
src/risk/**           (when added)
.env
.env.example
autofix.disable
kill.flag
AGENTIC_OPERATOR_PLAN.md
PHASE3_DESIGN.md
systemd/**
```

**Allowlist** (the prompt is also told these are the only files in scope, but the post-commit hook is the enforcement):

```
src/research/**
src/flows/**
src/tools/research_tools.py
src/tools/judas_detector.py
src/tools/db_tools.py
src/tools/session_tools.py
src/strategy_registry.py
src/dashboard/app.py
src/dashboard/templates/**
tests/**
```

The post-commit hook in the autofix worktree:
```bash
#!/usr/bin/env bash
denylist=$(cat .autofix-denylist)
changed=$(git diff --name-only HEAD^ HEAD)
for f in $changed; do
  for pattern in $denylist; do
    if [[ "$f" == $pattern ]]; then
      echo "DENIED: autofix attempted to modify $f"
      exit 1
    fi
  done
done
```

If the hook rejects, the commit fails, the worktree is cleaned up, and an `auto_fixes` row lands with `status='denied'`.

---

## Tables

```sql
CREATE TABLE IF NOT EXISTS auto_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    symptom_category TEXT NOT NULL,        -- one of the 5 above
    symptom_hash TEXT NOT NULL,
    symptom_summary TEXT NOT NULL,
    branch_name TEXT NOT NULL,             -- autofix/{utc}-{slug}
    worktree_path TEXT NOT NULL,
    prompt TEXT NOT NULL,                   -- full prompt sent to M2.7
    diff_summary TEXT,                       -- shortstat output
    files_changed_json TEXT,                 -- ["src/...", ...]
    test_result TEXT,                        -- "passed" | "failed" | "denied" | "timeout"
    test_output_tail TEXT,                   -- last 4KB of pytest output
    pushed BOOL DEFAULT FALSE,
    operator_decision TEXT,                  -- "merged" | "rejected" | null
    operator_decision_at_utc TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_fixes_active_symptom
    ON auto_fixes(symptom_hash) WHERE operator_decision IS NULL;
```

Unique partial index ensures only one open fix per symptom_hash at a time.

---

## Flow

```
fix_bug_step:
  1. Load symptom from state.findings["fix_bug"]
  2. Check trigger gates → if any False, log + return
  3. Compute symptom_hash; check uniqueness
  4. Insert auto_fixes row with status='running'
  5. Create git worktree:
        branch = autofix/{utc}-{symptom_slug}
        worktree_path = /tmp/jac-autofix-{id}
        git worktree add -b $branch $worktree_path master
        copy .autofix-denylist into worktree, install post-commit hook
  6. Invoke M2.7 harness with focused prompt + tools (30 turn / 30 min cap)
  7. After harness exits:
        a. cd worktree, git status — any changes?
        b. If yes: pytest -q in worktree
        c. If pytest passes: git commit (hook validates deny-list)
        d. If commit succeeds: git push origin $branch
        e. Update auto_fixes row with results
  8. Insert dashboard_notifications row with brief summary + link to GitHub PR
  9. Cleanup: git worktree remove (only on success or terminal failure; keep on partial)
```

Failure modes and handling:

| Failure | Handling |
|---|---|
| M2.7 timeout / 30-turn limit | mark `test_result='timeout'`, no commit, cleanup worktree, notification |
| M2.7 produces no patch | mark `diff_summary='empty'`, no commit |
| Patch fails to apply | mark `test_result='patch_failed'` |
| pytest fails | mark `test_result='failed'`, no commit, output_tail saved |
| Deny-list violation | post-commit hook rejects → mark `test_result='denied'`, alert |
| Push rejected | mark `pushed=false`, leave for operator |

---

## Operator UX (extends Phase 4 dashboard)

New "Auto-fix Queue" panel:
- List of `auto_fixes` rows with `operator_decision IS NULL`, sorted by recency.
- Each row: branch name, symptom summary, files changed, test result, link to GitHub branch + diff.
- Two buttons per row: **Merge to master** (calls `gh pr create` then merges, or `git merge` locally then push) and **Reject** (sets `operator_decision='rejected'`, deletes branch).

---

## Escalation to Claude Code

If M2.7 fails twice on the same `symptom_hash` (two `auto_fixes` rows with `test_result IN ('failed','timeout')`), the third trigger flag escalates to Claude Code instead. This requires:
- `claude` CLI authenticated non-interactively (uses host keychain or token file).
- Same worktree + deny-list + post-commit-hook structure.
- Higher per-attempt budget (60 min, more turns).
- Same `auto_fixes` row pattern with `executor='claude_code'`.

This path is opt-in via config flag `CLAUDE_CODE_ESCALATION_ENABLED` — start with M2.7-only and turn on escalation after observing.

---

## What I'm NOT doing in Phase 3

- No autonomous merge to master. Operator clicks merge.
- No ability to push to anywhere except `origin/autofix/*`.
- No editing of order-routing code, ever.
- No autofix during market hours, ever.
- No more than 3 autofixes per 24h.
- No rebases or force-pushes from autofix worktrees.

---

## Open questions for advisor

1. **Symptom detection placement** — should it live in `morning_review` (which runs once daily) or in a separate hourly hook that watches the runtime? Daily means a 23-hour MTTR for autofix; hourly means more flow churn.

2. **Test gate strictness** — pytest must pass before commit. But pytest takes ~12s today; that grows. At what duration does this become unworkable? Should we add a "fast" pytest subset for autofix gates?

3. **Branch lifecycle** — should rejected branches auto-delete on `operator_decision='rejected'`, or stay for forensics? My instinct: stay for 30 days, then prune.

4. **Concurrent symptoms** — what if `fix_bug_step` detects 3 different symptom_hashes in one pass? Sequential vs parallel? My instinct: queue them, process one per flow run — parallel autofixes share too much state risk.

5. **M2.7 unavailable / rate-limited** — fall back to deterministic "open a manual ticket" (just write the symptom to `auto_fixes` with `executor='manual'`)? Or escalate to Claude Code immediately?

6. **Autofix triggering autofix** — if an autofix introduces a regression that becomes a new symptom on the next flow run, do we autofix the autofix? My instinct: NO. Any symptom whose `symptom_hash` first appeared after an autofix-merge to master in the last 7 days routes to manual.

---

## Implementation phases (within Phase 3 itself)

To bound risk, build in 3 sub-phases:

**3a — table + skeleton + symptom detection (no LLM call yet)**
- `auto_fixes` table.
- `fix_bug_step` detects symptoms, creates worktree + branch, writes a stub `auto_fixes` row, surfaces on dashboard, but does not call LLM. Operator sees what symptoms it WOULD have tried to fix.
- Run for one week, observe.

**3b — M2.7 harness, no commit-push**
- Wire the M2.7 tools harness. Patch is generated, pytest is run, but `git commit` and `git push` are stubbed.
- Operator manually inspects the worktree diffs.
- Run for one week, observe.

**3c — full path with operator-merge gate**
- Enable git commit (with deny-list hook) + push to `autofix/*`.
- Dashboard merge button.
- Run with `MAX_AUTOFIXES_PER_DAY=1` initially.
