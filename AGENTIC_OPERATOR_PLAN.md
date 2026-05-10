# Agentic Operator Plan

**Status:** active spec, written 2026-05-09 after day-1 launch surfaced rough edges (3-hour CrewAI hierarchical loop, dry-run weekend-flatten on the workshop side, broker `dry_run` default-trap).

**Goal in one sentence:** an autonomous, paper-only futures lab on the IBKR paper account `DUH860616` that explores, validates, promotes, retires, and self-heals its own bugs within a bounded $5,000 sleeve — surfacing only big calls to the operator.

---

## North Star

> *"I have plenty of deterministic Python strategies. I want AI to run more of it, so I don't have to babysit. Full power to trade any futures inside the $5K paper sleeve. Full ability to write code, make changes, open and close anything."*  — Rob, 2026-05-09

Operationally that means: a typical week requires **under 30 minutes** of human review. Daily routine work — retiring underperformers, proposing experiments, fixing flaky tools, summarizing the trading day — runs without the operator in the loop. Big calls (promotion to live, novel order-path code, expanding the contract universe) still gate on `@human_feedback`.

---

## Authority Envelope (hard-coded boundaries)

These are the rails. Encoded in code, not just prompt.

| Capability | Allowed | Forbidden |
|---|---|---|
| Account mode | `paper` only — hard-locked in `src/config.py`, raises on anything else | live, real money |
| Account | IBKR paper `DUH860616` via `127.0.0.1:4002`, clientIds 150 (data) / 151 (exec) | any other account |
| Sleeve cap | `$5,000` (configurable in `config.yaml`); kill switch trips at `sleeve_drawdown_pct` (default 25%) | exceeding sleeve |
| Instruments | Any contract listed in `_CONTRACT_SPECS` in `src/portfolio_runtime.py`. Currently: `MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J`. **Adding a symbol is HITL by design** — requires updates to all three of `_CONTRACT_SPECS`, `ibkr_data.py`, `ibkr_executor.py` plus a regression test, gated by `@human_feedback`. The "trade any futures" intent is bounded to this universe; new symbols go through an explicit operator-approved expansion. | unrecognized symbols |
| Code-write inside `/home/rob/judas-agentic-crew` | Yes, on isolated branches via auto-fixer (Phase 3) | direct push to `master` from auto-fixer |
| Order-path files | **Write-protected from auto-fixer:** `src/tools/ibkr_executor.py`, `src/tools/ibkr_data.py`, `src/config.py`, `config.yaml`, anything under `src/risk/` (when added). Auto-fixer prompt has explicit deny-list; regression test asserts these files are unchanged in autofix commits | auto-fixer modifying these files |
| Cross-repo writes | None. Workshop repo `/home/rob/judas-futures-workshop` is **read-only** from agentic-crew | mutating workshop |
| Branch policy | Feature work on branches, `master` only after operator review. Push to GitHub gated on dashboard ack | force-push, history rewrite |
| Lucid / NT bridge | Stays parked. Reactivate per the workshop's `lucid_bridge_plan.md` only after a profitable paper week and explicit operator green-light | autonomous activation |

---

## Operating Rhythm

| Cadence | Job | Owner |
|---|---|---|
| Every 1H | `portfolio_runtime` scans `active_strategies`, evaluates, places paper brackets. Deterministic. | code |
| Hourly + on session opens | Reconcile IBKR positions vs DB. Halt new entries on mismatch. | code |
| Daily 06:00 ET | `OperatorFlow` runs: review yesterday → decide retire/keep → propose research → write daily brief → optionally request bug-fix | CrewAI Flow + M2.7 |
| Daily 04:00 local | DB backup | cron |
| Weekly Mon 06:30 ET | Regime review (vol regime, trending vs ranging, dominant winners) | OperatorFlow extension |
| Friday 15:55 CT | (Workshop only) weekend flatten — N/A for this repo | workshop |

**HITL checkpoints (where the operator is asked):**
1. Promoting a candidate to `active_strategies` (gates loosen with track record per Phase 6).
2. Merging an autofix branch to `master`.
3. Resetting kill switch after a drawdown trip.
4. Adding a new symbol to the contract universe.
5. Approving Lucid/NT bridge activation (separate document).

---

## Stack

```
┌─ Runtime orchestration ───────── CrewAI Flows
│   • @persist     — state across daily runs (SQLite outputs/flow_state.db)
│   • @human_feedback — promotion + autofix-merge gates
│   • @router / or_ / and_  — branching event graph
│   • Existing JudasCrew, ResearchCrew called as Flow steps
│
├─ Reasoning model ──────────────── MiniMax M2.7 (primary)
│   • SWE-Pro 56% / Terminal Bench 57% / Tool-call 75.8%
│   • ~2% of Opus 4.7 cost per token; can run hot
│   • Opus 4.7 reserved for high-stakes router decisions only
│
├─ Bug-fix delegation (Phase 3) ── Subprocess Claude Code OR Codex CLI
│   • Single session per fix, branch-isolated git worktree
│   • Escalate to Claude Code Agent Team for hard bugs only
│
├─ Persistence ─────────────────── judas_crew.db (SQLite, WAL mode)
│                                  outputs/flow_state.db (Flow @persist)
│
└─ Knowledge layer ──────────────── knowledge_base/*.md  +  skills.md
                                   (hand-rolled accumulator; not Hermes — revisit Hermes ≥ 6 months)
```

---

## Phases

Each phase has: **deliverables**, **exit criteria**, **owner**, **dependencies**.

### Phase 0 — Root-cause the sweep loop + audit-flagged criticals  (PRECONDITION)

**Why:** Codex's hardening (`Process.sequential`, hard timeout, `KillMode=control-group`, stale-PID reaper) makes the system *self-heal* but does not stop the underlying loop. The 45-min run on 2026-05-09 wrote zero `research_experiments` rows and burned the same Judas signals (`sweep_ts` 2025-11-11, 2026-04-17, 2026-04-27) on repeat. Without this, every later phase compounds on a research engine that produces nothing. Three audit-flagged correctness bugs ride along here because they touch the same files the agent is already in.

**Deliverables (P0a — sweep loop):**
- Diagnose the actual loop. **Start in `src/tools/research_tools.py`** but the agent must report and halt if the root cause turns out to be elsewhere (CrewAI hierarchical re-drive, MiniMax rate-limit retry, tool-arg validator, etc.) — *do not expand scope unilaterally*. If the loop is outside research_tools, the agent writes a 1-page diagnosis and exits; that diagnosis becomes a separate phase.
- If in scope: patch the root cause.
- Regression test: invoking `judas_threshold_sweep_tool` on a 30-day MGC slice returns structured results in **< 60 s** and writes exactly one `research_experiments` row.

**Deliverables (P0b — audit criticals, same agent, same worktree):**
- **Bracket order construction** — `src/portfolio_runtime.py:405-414`: replace `bracketOrder(limitPrice=0.0) + mutate-parent-to-MKT` with explicit `MarketOrder(parent) + StopOrder + LimitOrder` parented via `parentId`. Existing test suite must stay green.
- **Asyncio loop handling** — replace `asyncio.get_event_loop()` calls in `portfolio_runtime.py` (lines 377, 437), `ibkr_data.py:100`, `ibkr_executor.py:193` with `asyncio.new_event_loop()` or `asyncio.run`. Remove the misplaced `util.startLoop()` inside the async function in `portfolio_runtime.py:343`.
- **Non-atomic pair legs** — `_evaluate_pair` execution in `portfolio_runtime.py`: wrap the two-leg placement in a try/cancel block. If leg B fails to gate or place, cancel leg A's IBKR order and roll back the DB Position row.
- Each fix lands with a focused regression test.

**Exit criteria:**
- Test passes locally for sweep loop.
- All three audit-critical fixes have regression tests; existing tests stay green.
- One un-monitored timer firing produces a new row in `research_experiments`.
- `runtime_status.json` flips to `state: completed` (not `timed_out`).

**Owner:** parallel `general-purpose` agent on a worktree, supervised by me.

**Dependencies:** none.

**Scope guard:** if the loop fix balloons (e.g. requires architectural change to ResearchCrew), the agent reports and halts. Audit fixes ship regardless because they're independent.

---

### Phase 1 — OperatorFlow skeleton

**Why:** This is the persistent brain. Without it there's no scheduled, stateful daily review. Empty leaf steps for now — real logic lands in 2/4/5.

**Pre-flight:** verify `crewai` ≥ v1.8 in the agentic-crew venv (`pip show crewai`). **If pinned lower, bump `requirements.txt` to `crewai>=1.8` and commit this bump as the first commit of P1**, re-running the existing crew tests to confirm nothing breaks. We commit to using `@human_feedback` natively — no polling-table fallback, no "we'll see." If for any reason CrewAI 1.8+ breaks the existing crews, P1 halts and reports; we fix CrewAI compatibility before continuing.

**Deliverables:**
- New file `src/flows/operator_flow.py`:
  ```
  @persist  (sqlite at outputs/flow_state.db)
  class OperatorFlow(Flow[OperatorState]):
      @start
      def morning_review(self): ...        # calls ResearchCrew, returns findings
      @router(morning_review)
      def classify(self, findings): ...    # → "retire" | "explore" | "fix_bug" | "noop"
      @listen("retire")
      def retire_step(self, ...): ...      # stubbed; real in Phase 2
      @listen("explore")
      def explore_step(self, ...): ...     # stubbed; real in Phase 5
      @listen("fix_bug")
      def fix_bug_step(self, ...): ...     # stubbed; real in Phase 3
      @listen("noop")
      def write_brief_step(self, ...): ... # stubbed; real in Phase 4
  ```
- New systemd unit + timer: `judas-operator.service`, `judas-operator.timer` (daily 06:00 America/New_York with TZ-aware `OnCalendar`).
- New integration test: Flow loads, persists state, runs end-to-end with mocked steps in **< 60 s**, state survives a process restart (start, kill, resume, finish).

**Exit criteria:**
- `systemctl --user list-timers | grep judas-operator` shows the new timer.
- One manual run via `systemctl --user start judas-operator.service` completes successfully and writes a `flow_state.db` row.
- `pytest -q` green.

**Owner:** parallel `general-purpose` agent on a separate worktree, supervised by me.

**Dependencies:** Phase 0 must be merged first if the Flow's `morning_review` calls research — otherwise mock research in P1 and wire real research after P0 lands. **Both can run in parallel safely** because they touch zero overlapping files; merge order: P0 first (research engine works), then P1 (Flow calls it).

---

### Phase 2 — Live-performance demotion

**Why:** Closes the dangerous open loop. Today: promotion in, nothing out. Strategies degrade in live conditions and keep firing.

**Deliverables:**
- New file `src/research/live_review.py`:
  - `compute_live_metrics(strategy_id) → {pf_20, expectancy_20, max_consec_losers, days_since_last_fire, total_realized_pnl}`
  - `decide_action(metrics, regime, leaderboard) → Decision(action: keep|retune|retire, reason: str, confidence: float)` — calls M2.7 with deterministic threshold fallback (`pf_20 < 0.9 OR max_consec_losers ≥ 6 → retire`).
- Wire into `OperatorFlow.retire_step`. Atomic execution.
- **Audit fix** (kill two birds): inside `src/strategy_registry.py.promote_candidate`, prepend `BEGIN IMMEDIATE` and validate `params_json` schema before insert. Add regression test.
- Demotions auto-applied; promotions still gated by `@human_feedback`.
- **Demotion rollback:** `auto_demotions` preserves the original `params_json` and `metrics_json` snapshot. Operator can reactivate any retired row with one click on the dashboard — that re-inserts a fresh `active_strategies` row with the preserved params and a `reactivated_at_utc` audit field. Without this, a false demotion is permanent.

**Exit criteria:**
- Seeded bad strategy (rolling PF 0.4) auto-retires in one daily cycle.
- New test: concurrent promote/list under WAL produces no zero-active-strategy window.
- `auto_demotions` table records every retirement with reason + full params snapshot.
- Rollback test: retire a strategy, click reactivate, confirm it fires on the next portfolio scan.

**Owner:** TBD (probably parallel agent).

**Dependencies:** Phase 1 merged.

---

### Phase 3 — Code-fix delegation  (HIGHEST BLAST RADIUS — design first)

**Why:** This is what gets *me and Codex* out of the loop. When OperatorFlow detects a bug, it delegates a fix instead of pinging a human.

**Trigger gates (ALL must be true to fire):**
1. **No `autofix.disable` flag** in repo root. This file mirrors `kill.flag` — its presence halts all autofix activity at the top of `fix_bug_step`. The auto-fixer cannot delete this file (deny-list). One line of code, fully under operator control without editing source.
2. Market closed (CME futures) OR weekend.
3. Zero open positions (DB + IBKR cross-checked).
4. Failure pattern detected: 3 consecutive run errors of the same shape, OR a regression test failed in nightly, OR a known-bad signal in logs (e.g. `would_flatten` when not in dry-run).
5. No autofix branch already open for the same symptom hash.

**Deliverables:**
- `OperatorFlow.fix_bug_step`:
  - Spawns subprocess `claude code` (preferred — full coding tool harness) on a fresh `git worktree add` branch named `autofix/{utc_timestamp}-{symptom_slug}`.
  - Provides a focused prompt + a write-allowlist file (`autofix/.allowlist`) listing files the agent may touch.
  - **Deny-list (enforced by post-commit hook in the worktree):** `src/tools/ibkr_executor.py`, `src/tools/ibkr_data.py`, `src/config.py`, `config.yaml`, anything under `src/risk/`.
  - Runs `pytest -q` in the worktree before considering done.
  - Records `auto_fixes` row: `(branch, prompt, diff_summary, files_changed, test_result, started_at, finished_at)`.
  - Pushes branch to `origin` (NEVER `master`) and writes a notification row in `dashboard_notifications`.
  - HITL: operator acks via dashboard; merge to `master` is a separate human-triggered step.
- New tables: `auto_fixes`, `dashboard_notifications`.
- New post-commit hook in the autofix worktree that fails the commit if any deny-listed file changed.
- Hard timeout per autofix: **30 min** wall clock; killed via `KillMode=control-group`.

**Exit criteria:**
- Synthetic broken tool (e.g. `judas_threshold_sweep_tool` returns `None` instead of dict) gets diagnosed, patched, regression-tested, committed to `autofix/...`, pushed to origin, and shows on dashboard with diff preview — all without human input.
- Operator can ack with one click; merge happens on dashboard ack only.
- An autofix attempt that touches a deny-listed file fails the commit hook and aborts cleanly.

**Owner:** dedicated session, advisor-reviewed before build.

**Dependencies:** Phase 1 merged, Phase 4 dashboard surface (for the ack UI).

---

### Phase 4 — Daily brief + dashboard surface

**Why:** Turns raw data into operator-actionable intelligence. The thing you actually wanted from "read the moves for the day and adjust."

**Deliverables:**
- `OperatorFlow.write_brief_step`:
  - Pulls last 24h fires, fills, P&L by strategy from `trades`.
  - Tags regime: `{vol_regime: high|mid|low, trend: trending|ranging, leaders: [sym, ...]}`.
  - Identifies surprises: P&L > 2σ from backtest expectancy.
  - Writes `daily_briefs` row + Markdown to `outputs/briefs/YYYY-MM-DD.md`.
- Dashboard updates (`src/dashboard/app.py` + frontend):
  - "Today's Brief" panel showing latest entry.
  - Action items with one-click ack/reject.
  - Autofix notifications panel (links to Phase 3 outputs).

**Exit criteria:**
- Saturday morning, dashboard shows a populated brief for Friday + ack buttons live.

**Owner:** TBD.

**Dependencies:** Phase 2 (so demotion-recommendations exist to surface).

---

### Phase 5 — Adaptive exploration planner

**Why:** Without this, research forever sweeps the same Judas params on MGC. With it, research direction adapts to gaps + regime.

**Deliverables:**
- `OperatorFlow.explore_step`:
  - Agent reads: `active_strategies` snapshot, workshop leaderboard CSVs, recent regime tags from Phase 4, `auto_demotions` graveyard.
  - Outputs an experiment plan: `{tool: judas_threshold_sweep|pair_sweep|rsi_grid, symbol: <str>, params: {...}, rationale: <str>}`.
  - Deterministic tool runs the experiment.
  - Result flows through Phase 2's promotion path.

**Exit criteria:**
- Over a 7-day rolling window, **≥ 30% of `active_strategies` rows turn over** (retired + replaced via the explore→promote path), with retire/replace decisions traceable to a regime tag in `auto_demotions.reason` or `strategy_candidates.rationale`.

**Owner:** TBD.

**Dependencies:** Phase 4 (regime tags), Phase 2 (promotion path).

---

### Phase 6 — Shrink the human-in-the-loop

**Why:** End-state. The system earns autonomy by demonstrating safety.

**Deliverables:**
- Promotion gate auto-loosens with track record:
  - First 5 promotions: explicit `@human_feedback` ack required.
  - Promotions 6+: if last 5 promoted strategies have rolling PF ≥ 1.0, auto-promote with notification only. Reset to manual on any promotion that produces a 5-trade losing streak.
- Autofix gate stays for order-path edits (deny-list files) **forever**. For non-order-path edits, loosens after 10 successful auto-applied fixes with no regression.
- Demotions: never blocked.

**Exit criteria:**
- Typical week requires < 30 min of human review.

**Owner:** TBD.

**Dependencies:** Phases 2, 3 stable for ≥ 4 weeks.

---

## Safety Rails (always-on, code-enforced)

1. `mode='paper'` check in `src/config.py` — auto-fixer cannot edit (deny-list).
2. `kill.flag` in repo root halts trading on next tick (existing). Auto-fixer cannot delete.
3. Sleeve drawdown auto-halt at `sleeve_drawdown_pct` (existing).
4. Per-strategy `already_open` gate (existing).
5. `max_open_positions` cap (existing).
6. Hourly reconcile against IBKR positions; halt new entries on mismatch (existing).
7. `@human_feedback` gates: promotions, autofix-branch merges, kill-switch resets, contract-universe expansion.
8. `auto_demotions` table is append-only — provides audit trail.
9. Daily backup of `judas_crew.db` (cron, existing for workshop; replicate for crew DB).
10. **`autofix.disable` flag** in repo root halts all auto-fixer activity. Mirrors `kill.flag`. Auto-fixer cannot delete (deny-list). See Phase 3 trigger gates.
11. **Audit-flagged correctness fixes** are deliverables of Phase 0b (no longer "housekeeping" — they ship with own regression tests as part of the precondition phase). See Phase 0 for details.

---

## What I Will Not Build

- **LLM-driven order sizing.** Sleeve sizing is solved math.
- **LLM-driven stop/target placement.** Deterministic only.
- **Automatic Lucid/NT bridge activation.** Stays parked per workshop memory.
- **Self-modifying order routing code.** Deny-list enforced by post-commit hook.
- **Promotion of strategies whose backtest expectancy is negative.** Deterministic block before LLM is asked.
- **Cross-repo writes.** Workshop is read-only from agentic-crew.

---

## Sequencing

| When | Phases | How |
|---|---|---|
| Today | **P0 + P1** | Two parallel `general-purpose` agents on isolated worktrees → review → merge to `master` → push |
| Next session | **P2** | Single agent, Phase 1 must be merged |
| Following sessions | **P3, P4, P5, P6** | One per session; **P3 design reviewed by advisor before code** |

---

## Open Questions (resolved before P3)

- Which subprocess: `claude code` or `codex` CLI for autofix? Lean Claude Code (Agent Teams available for hard bugs), but verify both authenticate non-interactively in a worktree.
- Where does dashboard ack live? Existing dashboard `:8080`/`:8443` — new `/api/notifications` endpoints + UI panel.
- Autofix prompt template — needs design pass; not a one-liner.
- How does `@human_feedback` deliver questions when the operator is offline? — Polling table is the fallback; investigate native CrewAI webhook support first.

---

## Drift Prevention

This file is the spec. Any phase that lands without a matching update here is incomplete. Updates to scope require:
1. Edit this file.
2. Note the change at the top under `Status:`.
3. Re-run advisor review on material changes.
