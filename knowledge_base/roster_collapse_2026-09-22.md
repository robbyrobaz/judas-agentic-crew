# Roster collapse 2026-09-05 → 09-20 (25 → 4 actives) — root cause + guardrails

**Written by Rob via Claude, 2026-09-22 (MST).** Read this before changing any
agent budget, promotion gate, or retirement rule.

## What happened
- Active roster: 21 (Sep 1) → 25 (Sep 5) → 16 (Sep 10) → 8 (Sep 16) → 4 (Sep 20).
- Candidate inflow (`strategy_candidates` rows/week): 60-140/wk through late Aug,
  **6 in W36, 2 in W37, zero after Sep 16.**
- Retirements kept running at the old pace (~3 every other day), each with a
  reasonable-sounding note ("empty slot is safer than a net-loser", stale-fire,
  thin sample, regime). Nobody was refilling.

## Why inflow died — commit 16a0755 (2026-09-02, "Restore SimJudasFutures runtime")
1. Researcher run caps: **unlimited → 12 turns / 900 s / 750k tokens per run**
   (`scripts/run_researcher.py`, `agent_runner._DEFAULT_RUN_TOKEN_CAP`). Daily
   budget 150M → 20M. The researcher's prompt orders it to work tasks, then
   YouTube-ingest 4-8 videos, then web-search, then backtest, then sweep 5 symbols,
   then propose. That is 20+ tool calls; under 12 turns it hit the token cap by
   turn ~7 every run and never reached `propose_candidate`. 61 of 70 researcher
   tasks since Sep 1 were abandoned as "claimed and left unfinished".
2. Promotion gate tightened at the same time: n >= 20 (was 15), PF >= 1.3, and
   **custom evidence must be stamped `cost_model=v1_realistic_micros`**. Only 8 of
   269 `custom_strategies` carry that stamp, so the whole library became
   "research-only" overnight. Nothing re-backtested it.
3. `reactivate_demoted` is blocked for anything retired as a duplicate-fire pair,
   and there was no other path back for 4,391 retired strategies.
4. No process measured roster size. The reviewer/registrar/operator prompts all
   said "an empty slot is safer than a net-loser" with no floor.

## Guardrails now in code (2026-09-22)
- `strategy_registry.roster_floor()` (env `JUDAS_ROSTER_FLOOR`, default 10) and
  `roster_health()`. **`retire_strategy()` refuses below the floor** unless the
  strategy is a code-verified hard loser from live fills (pf_20 < 0.9 on n >= 10,
  or 6+ consecutive losers) or a structural retire (duplicate-fire, banned symbol).
  The LLM's reason text is not evidence. `force=True` exists for humans/scripts
  only — never expose it to an LLM tool. Tests: `tests/test_roster_floor.py`.
- Every agent briefing carries a `ROSTER HEALTH` line (n_active, floor,
  uncovered symbols, candidates_3d/7d, retired_7d, INFLOW STALLED).
- Researcher briefing carries a `LIBRARY` block (best prior backtests per thin
  symbol from `custom_strategies` + soft-retired actives) and its prompt now
  runs **ROSTER REFILL first** when below floor/uncovered/stalled; YouTube ingest
  only when the roster is healthy. `run_custom_backtest` produces the v1 stamp,
  so re-qualifying library members is 1 call per symbol.
- Researcher caps: 40 turns / 2400 s / 4M tokens per run
  (`systemd/judas-researcher.service` Environment=), daily budget 60M.
- `scripts/roster_watchdog.py` + `judas-roster-watchdog.timer` (05:00 and 21:00
  UTC): records a `roster_watchdog` finding and queues one high-urgency
  `refill_roster` researcher task per thin symbol whenever the book is below
  floor, a legal symbol is uncovered, or no candidate was proposed in 3 days.

## Rules so this does not happen again
1. **Never cap or re-order an agent without measuring its output for a week.**
   The metric for the researcher is `strategy_candidates` rows/day, not
   "success=True" in the journal. A cycle that ends on a turn/token cap with
   `actions=0..2` is a failed cycle.
2. **Never raise a promotion gate without a migration for the existing library.**
   New evidence requirements (like the v1 cost stamp) must ship with a script or
   a researcher task that re-qualifies what already exists.
3. **Pruning must be paired with refill.** Any change that makes retiring easier
   must show where replacements come from. Below the floor, code refuses soft
   retires — do not add a bypass to the LLM tool surface.
4. **Watch the roster, not the P&L.** `roster_health()` is the check; the
   watchdog finding and `ROSTER HEALTH` line are where agents see it.
5. Mass edits touching >30 files (16a0755 touched 37) get a one-line-per-file
   review before commit. The three changes that caused this were in three
   different files of the same commit.
