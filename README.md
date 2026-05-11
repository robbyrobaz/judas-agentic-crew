# judas-agentic-crew

A self-running, paper-only futures lab on IBKR that **explores, validates, promotes, retires, and self-heals its own bugs** within a bounded $5,000 paper sleeve. Operator reviews ~once a day; the system handles the rest.

```
┌─ MiniMax M2.7 (reasoning) ─┐    ┌─ Deterministic Python ─┐    ┌─ IBKR paper ─┐
│ • per-strategy retire calls│ →  │ • sleeve sizing         │ →  │ DUH860616     │
│ • exploration planning     │    │ • bracket order placer  │    │ port 4002     │
│ • code-fix harness (P3)    │    │ • atomic registry       │    │ clientId 150  │
└────────────────────────────┘    └─────────────────────────┘    └───────────────┘
```

The companion repo `../judas-futures-workshop` is the **seeded incumbent baseline** — its PF-ranked buffet strategies are imported here and continuously challenged by research. Workshop is read-only from this repo.

---

## Architecture (Phase 10 — agentic team)

The single "PM agent with a fat tool palette" of Phases 8–9 is superseded.
The Operator is a **manager that delegates**. Specialists do the work.

```
                      Operator (manager) — every 4h, ~20 turns
                          │  brain. decides what should happen.
                          │  tools = DELEGATIONS, not actions.
                          │
            ┌─────────────┼─────────────┬─────────────┐
            ▼             ▼             ▼             ▼
        Researcher     Trader      Registrar       Coder
        ─────────      ──────      ─────────       ─────
        long-cycle     short       short           on-demand
        ingest web/yt  ~10 turns   ~5 turns        ~30 turns
        backtest       executes    atomic retire/  Phase 3 autofix
        propose        a single    promote/        already built
        candidates     trade       modify
```

### How delegation works

1. Operator runs every 4h via `judas-operator.timer`. Its tool palette is
   four `delegate_to_*` tools plus reads — no direct retire/promote/
   place_bracket/ingestion.
2. Each `delegate_to_*` call inserts a row into the shared `agent_tasks`
   table tagged with `team`, `action`, `payload_json`, `urgency`,
   `status='open'`.
3. Specialists run on their own cadence (`judas-researcher.timer`,
   `judas-trader.timer`, `judas-registrar.timer`). Each pulls open tasks
   for its team via `get_open_tasks`, claims via `claim_task`, executes,
   then `complete_task` writes `result_json`.
4. The Coder is on-demand — fired when the Operator delegates, runs the
   existing Phase 3 autofix harness for one symptom per cycle.
5. The daily brief (`outputs/briefs/YYYY-MM-DD.md`) shows what got
   delegated, what came back, and the usual P&L/regime/surprises.

### Tool palettes

**Operator** — delegations + reads only:
`delegate_to_researcher`, `delegate_to_trader`, `delegate_to_registrar`,
`delegate_to_coder`, `get_active_strategies`, `get_recent_pnl`,
`get_recent_briefs`, `get_outstanding_delegations`, `get_recent_trades`,
`get_candidates_queue`, `get_workshop_leaderboard`, `query_db`,
`get_strategy_detail`, `get_recent_experiments`, `get_open_positions`,
`get_regime_tag`.

**Researcher** — ingestion + backtests + proposals only:
`get_active_strategies`, `get_strategy_detail`, `get_workshop_leaderboard`,
`get_candidates_queue`, `get_recent_pnl`, `get_regime_tag`,
`get_recent_briefs`, `get_recent_experiments`, `query_db`, `web_search`,
`web_fetch`, `fetch_youtube_transcript`, `search_youtube_trading_videos`,
`read_file`, `list_files`, `read_research_artifact`,
`run_judas_threshold_sweep`, `run_walk_forward`, `run_custom_backtest`,
`propose_candidate`, `propose_custom_strategy`, `claim_task`,
`complete_task`, `get_open_tasks`.

**Trader** — execute + cancel + report fills:
`place_bracket_order`, `cancel_order`, `get_open_positions`, `get_fills`,
`get_recent_pnl`, `claim_task`, `complete_task`, `get_open_tasks`.

**Registrar** — atomic registry mutations only:
`retire_strategy`, `promote_candidate`, `modify_strategy_params`,
`reactivate_demoted`, `get_active_strategies`, `get_candidates_queue`,
`get_strategy_detail`, `claim_task`, `complete_task`, `get_open_tasks`.

**Coder** — Phase 3 autofix harness consumer (no direct LLM tool palette, no timer). Invoked synchronously when the Operator calls `delegate_to_coder(symptom, context)`; the call inserts the symptom on `auto_fixes` and runs worktree → M2.7 patch → pytest → commit + push inline, updating the `agent_tasks` row with the result.

**Shared by all agents:** `record_finding`, `read_findings`,
`retract_finding`, `get_strategy_dossier` — the team's persistent memory
log. Each agent reads recent findings at the start of its cycle and
records new ones as it discovers things worth remembering across cycles.

---

## What's Built (Phases 0–10 shipped)

| Phase | Feature | Status |
|---|---|---|
| **0** | Sweep loop fix + 3 audit-critical correctness fixes (bracket construction, asyncio loops, atomic pair legs) | ✅ |
| **1** | `OperatorFlow` skeleton — CrewAI Flow with `@persist` SQLite-backed state, daily systemd timer | ✅ |
| **2** | Live-performance demotion — rolling 20-trade PF/expectancy → retire decisions, atomic registry, `auto_demotions` ledger with one-click reactivate | ✅ |
| **3** | Code-fix delegation — symptom detection, M2.7 autofix harness with tool palette, branch-isolated worktree, deny-list post-commit hook, dashboard merge/reject | ✅ |
| **4** | Daily brief — composes 24h fires/fills/PnL/regime/surprises/recommendations, persists Markdown + DB row, dashboard panel | ✅ |
| **5** | Adaptive exploration planner — M2.7 picks tool/symbol/params with deterministic fallback, dispatches sweeps + walk-forward | ✅ |
| **6** | HITL shrinkage — promotion gate auto-loosens with track record (policy, incremental) | 🟡 |
| **7** | Dashboard surface — auto-fixes queue, demotions ledger, live perf grid, regime ribbon, system health bar (designed in `DASHBOARD_PROPOSAL.md`) | 📋 |

Tests: **97/97 passing.** Wall-clock to ship 0/1/2/4/5 + 3 design + 3 a/b/c full wire-up: ~3 hours via parallel worktree-isolated worker agents.

---

## Daily Routine

The system runs on three independent cadences:

| Cadence | Service / Timer | What it does |
|---|---|---|
| Hourly | `judas-crew.timer` (existing trading runtime) | Scans `active_strategies`, evaluates each on fresh 1H bars, places paper bracket orders. **Pure deterministic Python** — no LLM in the order path. |
| Hourly (weekends) / nightly (weekdays) | `judas-researcher.timer` | Researcher specialist — ingests web/YouTube/files, runs backtests, proposes candidates. Replaces the old `judas-research.timer`. |
| Every 5 min | `judas-trader.timer` | Trader specialist — pulls queued trades and places brackets. |
| Every 5 min | `judas-registrar.timer` | Registrar specialist — applies queued retire/promote/modify atomically. |
| **Daily 06:00 ET** | **`judas-operator.timer`** (the brain) | The OperatorFlow morning review — see below. |
| Always-on | `judas-dashboard.service` | Flask backend + React frontend on `:8080` |

### What the OperatorFlow does each morning

```
06:00 ET  ── morning_review
              │  for each of N active_strategies:
              │    compute rolling 20-trade PF / expectancy / max-consec-losers / days-since-fire
              │    M2.7 decides: keep | retune | retire (deterministic fallback always available)
              │  also detects symptoms (tool failures, looping research, naked positions, etc.)
              ▼
          classify → routes to:
              ├─ retire_step    → atomically retire each flagged strategy via auto_demotions
              ├─ explore_step   → M2.7 picks experiment plan, dispatches sweep / walk-forward
              ├─ fix_bug_step   → symptom → worktree → M2.7 harness → commit autofix branch → push
              └─ noop           → fall through
              ▼
          write_brief_step (always runs)
              composes daily Markdown brief, persists to daily_briefs + outputs/briefs/YYYY-MM-DD.md,
              surfaces on dashboard with Apply/Reject buttons next to each recommended action
```

Errors at any step are logged and swallowed — the brief always writes. The flow never crashes.

### Heavy research blitz (manual)

`scripts/research_blitz.py` cycles through every active symbol back-to-back via `research_tick`. Each run is bounded by the existing flock + 45-min hard timeout + stale-PID reaper. Designed for nohup over multi-hour windows when you're away from the desk:

```bash
nohup .venv/bin/python scripts/research_blitz.py --hours 22 --interval-min 0.5 \
    > logs/research_blitz.log 2>&1 &
```

A typical 22-hour run produces ~50–100 fresh `research_experiments` rows.

---

## Authority Envelope (hard-coded)

| Capability | Allowed | Forbidden |
|---|---|---|
| Account mode | `paper` only — hard-locked in `src/config.py`; raises on anything else | live, real money |
| Sleeve cap | `$5,000` configurable; kill switch trips at `sleeve_drawdown_pct` (default 25%) | exceeding sleeve |
| Instruments | MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J. Adding a symbol is HITL-gated | unrecognized symbols |
| Code-write | autofix on isolated branches; **never** to `master` without operator merge click | direct master push from autofix |
| Order-path files | **Write-protected from autofix:** `ibkr_executor.py`, `ibkr_data.py`, `config.py`, `config.yaml`, `src/risk/**`. Enforced by post-commit deny-list hook | autofix touching these |
| Cross-repo | Workshop is read-only | mutating workshop |
| Lucid / NT bridge | Stays parked. Reactivate via `lucid_bridge_plan.md` | autonomous activation |

### Always-on safety rails

1. `mode='paper'` check raises on non-paper at construction.
2. `kill.flag` in repo root halts trading on next tick.
3. `autofix.disable` flag halts the autofix path. Mirrors `kill.flag`.
4. `JUDAS_AUTOFIX_INHIBIT=1` env short-circuits autofix dispatch (used in tests).
5. Sleeve-drawdown auto-halt.
6. Per-strategy `already_open` gate prevents stacking.
7. `max_open_positions` cap.
8. Hourly reconcile against IBKR positions; halt new entries on mismatch.
9. `@human_feedback` gates: promotions, autofix-branch merges, kill-switch resets, contract-universe expansion.
10. `auto_demotions` is append-only — full audit trail with one-click reactivate.

---

## Layout

```
src/
  flows/operator_flow.py         OperatorFlow brain (CrewAI Flow + @persist)
  research/
    live_review.py               Phase 2 — rolling metrics + decide_action (M2.7 + det. fallback)
    brief.py                     Phase 4 — compose + persist daily brief
    regime.py                    Phase 4 — vol / trend / leaders tagger
    explore.py                   Phase 5 — context + plan + execute experiment
    symptoms.py                  Phase 3a — 5 symptom detectors
    autofix_harness.py           Phase 3b — M2.7 harness with tool palette + budgets
    autofix_executor.py          Phase 3c — worktree, deny-list hook, commit + push
  strategy_registry.py           atomic promote / retire_strategy / reactivate_demoted
  portfolio_runtime.py           hourly trading runtime (deterministic)
  tools/                         judas detector, IBKR data/exec, db tools, session tools
  agents/                        CrewAI agents (research crew + operator manager)
  crews/                         JudasCrew (legacy 4-step) + ResearchCrew
  dashboard/app.py               Flask + endpoints for briefs / demotions / autofixes
  db/models.py                   SQLite schema (raw SQL + init_db)

scripts/
  research_blitz.py              cycle research across all active symbols, hours-long
  research_tick.py               one ResearchCrew tick with flock + timeout + reaper
  run_research.py                ResearchCrew kickoff for a single symbol
  import_workshop_seed.py        one-shot baseline import from ../judas-futures-workshop

systemd/
  judas-crew.{service,timer}             hourly trading
  judas-research.{service,timer}         weekend research
  judas-operator.{service,timer}         daily 06:00 ET brain
  judas-dashboard.service                always-on Flask + frontend
  install.sh                             rootless --user install

knowledge_base/
  buffet.yaml                            workshop top-PF strategies
  judas_concepts.md                      ICT Judas Swing reference
  market_hours.md
  research_findings.md
  workshop_context.md

dashboard/                               React + TypeScript + Tailwind frontend

tests/                                   97 tests across all phases

AGENTIC_OPERATOR_PLAN.md                 the spec — drift-prevention rule
PHASE3_DESIGN.md                         self-modifying-code design (advisor-reviewed)
DASHBOARD_PROPOSAL.md                    Phase 7 panel spec
HANDOFF_2026-05-10.md                    operator handoff log
```

---

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd dashboard && npm ci && npm run build && cd ..

# Env
cat > .env <<EOF
MINIMAX_API_KEY=...
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_DATA_CLIENT_ID=150
IBKR_EXEC_CLIENT_ID=151
EOF
```

Install systemd units (all four, including the new operator brain):

```bash
bash systemd/install.sh
```

That script also imports the workshop baseline + builds the frontend.

---

## Common Operations

### Health check

```bash
.venv/bin/python main.py --doctor --symbol MGC
```

### Manual trigger (right now)

```bash
systemctl --user start judas-operator.service        # full daily review
systemctl --user start judas-researcher.service      # one researcher cycle
systemctl --user start judas-trader.service          # drain queued trades
systemctl --user start judas-registrar.service       # apply queued mutations
systemctl --user start judas-crew.service            # one trading scan
```

### Inspect what's happening

```bash
# Today's brief (if generated)
sqlite3 judas_crew.db "SELECT brief_date, length(content_md), substr(summary_json, 1, 200) FROM daily_briefs ORDER BY id DESC LIMIT 1;"

# Auto-demotions ledger
sqlite3 judas_crew.db "SELECT id, ts_utc, symbol, strategy_family, reason, reactivated_at_utc FROM auto_demotions ORDER BY id DESC LIMIT 10;"

# Auto-fixes queue
sqlite3 judas_crew.db "SELECT id, started_at_utc, symptom_category, status, test_result, pushed, operator_decision FROM auto_fixes ORDER BY id DESC LIMIT 10;"

# Recent research experiments
sqlite3 judas_crew.db "SELECT id, ts_utc, experiment_type, status FROM research_experiments ORDER BY id DESC LIMIT 10;"

# Research blitz progress
tail -10 logs/research_blitz.log
tail -5 outputs/research/blitz_log.jsonl

# Active strategies
sqlite3 judas_crew.db "SELECT symbol, strategy_family, version, state FROM active_strategies WHERE state='active';"

# Timer health
systemctl --user list-timers | grep judas-

# OperatorFlow last run state
sqlite3 outputs/flow_state.db ".tables"
```

### Operator overrides

```bash
# Halt all trading next tick
touch kill.flag

# Halt only the autofix path (system keeps trading and reviewing)
touch autofix.disable

# Reactivate a demoted strategy via the dashboard (preferred)
curl -X POST http://127.0.0.1:8080/api/demotions/<id>/reactivate

# Apply a recommended action from the brief
curl -X POST http://127.0.0.1:8080/api/briefs/<YYYY-MM-DD>/recommended/<index>/apply

# Merge an autofix branch after reviewing on GitHub
curl -X POST http://127.0.0.1:8080/api/autofixes/<id>/merge
```

### Dashboard

- `http://omen-claw.tail76e7df.ts.net:8080/` (tailnet)
- `http://127.0.0.1:8080/` (local)

Today's Brief panel sits at the top with Apply / Reject buttons on each recommended action. More panels (auto-fixes queue, demotions ledger, live perf grid, regime ribbon, system health bar) are designed in `DASHBOARD_PROPOSAL.md` — Phase 7.

---

## How Promotion / Demotion Works

**Promotion** (still operator-gated for now):
1. Research runs a sweep (or explore_step picks a target).
2. Walk-forward validation checks deterministic thresholds (PF ≥ 1.3, expectancy ≥ 0.15).
3. A `strategy_candidates` row is created.
4. `promote_candidate(id)` atomically retires the prior version and inserts a v+1 active row. Wrapped in `BEGIN IMMEDIATE` with `params_json` schema validation.
5. `@human_feedback` gates the operator click (loosens with track record per Phase 6).

**Demotion** (auto, the closed-loop fix):
1. Daily morning_review computes live metrics from `trades`.
2. M2.7 (or deterministic fallback) decides keep | retune | retire per strategy.
3. `retire_step` atomically: marks active row retired AND inserts `auto_demotions` row preserving full `params_json` snapshot.
4. Operator can one-click reactivate via dashboard if they disagree — the snapshot is the source of truth.

**Code-fix** (Phase 3 harness, Phase 10 dispatch — fully autonomous):
1. Operator agent calls `delegate_to_coder(symptom, context)` — there is **no Coder timer** by design; the delegation IS the trigger. The same path is also driven by `OperatorFlow.fix_bug_step`'s symptom detector, via the shared `src/research/autofix_dispatch.py` module.
2. `auto_fixes` row inserted with `status='detected'` (deduplicated by `symptom_hash`).
3. Trigger gates: `autofix.disable` absent + market closed + 0 open positions. **No daily cap** — `MAX_AUTOFIXES_PER_DAY = 0` disables it; set it to a positive integer to re-enable a ceiling. `JUDAS_AUTOFIX_INHIBIT=1` short-circuits dispatch (used by tests).
4. Worktree created at `/tmp/jac-autofix-<id>` on branch `autofix/<utc>-<slug>`.
5. Post-commit deny-list hook installed (rejects any commit touching order-path files).
6. M2.7 harness runs with tools: `read_file`, `list_files`, `grep`, `apply_patch`, `run_tests`, `git_status`, `git_diff`. 30 turn / 30 min budget.
7. If patch + pytest passes → `git commit` (hook validates) → `git push origin autofix/<...>`.
8. Operator reviews diff on GitHub, clicks Merge or Reject in dashboard.

---

## Goal

The system is not a static replica of the workshop. It's a **learner**:

- Workshop is the seeded incumbent.
- Research continuously challenges it.
- Promotion adds stronger variants; demotion retires weaker ones.
- Autofix patches the system itself when something breaks.
- Operator reviews aggregates, not individual trades.

Over time, the active set should outperform the seed. That's the only metric that matters.

---

## Sister Repos

- `../judas-futures-workshop` — the deterministic Python workshop. Read-only baseline. Has its own systemd units (`judasfutures-*`) and DB (`judasfutures.db`). Different clientIds (137/138 vs 150/151) so we coexist on the same paper account.

## Drift Prevention

Any phase that lands without a matching update to `AGENTIC_OPERATOR_PLAN.md` is incomplete. The plan is the spec; the README is the marketing.
