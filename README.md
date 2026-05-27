# judas-agentic-crew

A self-running paper futures lab on IBKR that continuously ingests trading content, backtests ideas, and promotes/retires strategies — all without human intervention. The scanner that places trades is **pure deterministic Python with zero LLM calls**. The LLM budget is spent entirely on research.

---

## Current State (2026-05-27)

### Active Strategies (6)

| ID | Symbol | Family | Type | Notes |
|---|---|---|---|---|
| 3228 | MNQ | judas_1h | judas_native | Sweep+CHoCH, displacement filtered |
| 3240 | MGC | judas_1h | judas_native | Sweep+CHoCH |
| 3250 | DX | judas_1h | judas_native | Sweep+CHoCH |
| 3271 | MET | buffet_zoo | RSI 30/70 | 98 fires/7d in workshop (5m bars → expect 2-5/week on 1H) |
| 3272 | MCL | buffet_zoo | Bollinger bb_20 | 18 fires/7d workshop |
| 3273 | MCL | zoo | MA cross 9/21 | 8 fires/7d workshop |

**judas_native** strategies fire on sweep+CHoCH patterns (ICT Judas Swing, inherently low-frequency). **buffet_zoo/zoo** strategies fire on RSI/BB/MA crossovers — fire more often, fully validated in workshop.

### Contract Expiries

| Symbol | Contract | Expiry | Notes |
|---|---|---|---|
| MET | METK6 | 2026-05-29 | Auto-rolls when next-month volume flips |
| DX | DXM6 | 2026-06-15 | |
| MNQ | MNQM6 | 2026-06-18 | |
| MCL | MCLN6 | 2026-06-18 | |
| MGC | MGCM6 | 2026-06-26 | |

**Volume-based roll**: When a contract is ≤14 days from expiry, `bar_cache._pick_contract()` fetches 10-bar volume for both front and next month and rolls to next if next-month volume is higher. Logs `bar_cache.rolling` or `bar_cache.no_roll`.

---

## The Design Philosophy: Burn the 45,000 Requests Productively

This system runs on MiniMax M2.7 with **45,000 API requests/week**. The goal is NOT to conserve them — it's to spend every request on something that produces alpha: YouTube ingestion, backtesting, strategy proposals.

**The key principle: large kickoff prompt + action-only tools = every request does real work.**

Each Researcher session pre-loads the full context (active strategies, open tasks, recent findings, pending candidates, last brief) into the first message before any LLM call. The LLM never needs to ask "what strategies are active?" — it already knows. Every one of its turns goes straight to YouTube search, backtest run, or proposal submission.

**Target: 25,000–35,000 requests/week, all on productive researcher work.**

```
Request allocation target:
  Researcher (YouTube → backtest → propose)  18,000–22,000 req/week  ← the usage engine
  Operator (2×/day portfolio review)          3,000– 4,000 req/week
  On-demand bursts (dashboard triggers)       2,000– 5,000 req/week
  Infrastructure (registrar, trader, coder)      < 500 req/week
  Scanner (hourly trading)                            0 req/week  ← zero LLM, always
```

---

## Architecture

```
judas-crew.timer (hourly)
  └─ portfolio_runtime.py — ZERO LLM — pure Python
       ├─ refresh bar cache from IBKR
       ├─ reconcile open trades (stop/target hit?)
       └─ evaluate active_strategies → place_bracket()

judas-researcher.timer (every 90 min market hours + 2× nightly)
  └─ researcher_agent.py — THE USAGE ENGINE
       ├─ _build_kickoff(db_path) — pre-load context into first message
       └─ LLM loop: YouTube search → transcript → backtest → propose/reject
            target: 7 requests → 40 actions per session

judas-operator.timer (06:00 + 21:00 UTC)
  └─ operator_agent.py — reads everything, delegates, writes brief

judas-registrar.timer (06:30, 13:30, 21:30 UTC)
  └─ registrar_agent.py — short queue-flush: promote approved candidates, retire dead strategies

judas-trader.timer (hourly, self-gating)
  └─ trader_agent.py — skips in <100ms if no pending trader tasks

judas-coder.timer (hourly, self-gating)
  └─ coder_agent.py — skips in <100ms if no pending coder tasks

judas-dashboard.service (always-on, port 5080)
```

---

## How the Researcher Session Works

The Researcher is the heart of the system. It runs ~8 times/day during weekdays.

### Pre-load (before first LLM call)

`_build_kickoff(db_path)` queries the DB and injects into the first user message:
- All active strategies with their family, version, symbol, and state
- Open tasks assigned to the researcher team
- Last 10 findings (video IDs already processed, rejected concepts, accepted ones)
- Pending strategy candidates awaiting promotion
- Last daily brief summary
- Symbols with zero coverage (research priority)

This is why the LLM can hit the ground running on turn 1 instead of spending 3-4 turns on discovery tool calls.

### Session loop (every run)

```
1. INGEST
   search_youtube_trading_videos("ICT liquidity sweep 2026")
   search_youtube_trading_videos("SMC order block judas swing")
   → Pull top 4-8 video URLs from last 24-48h
   → Check findings: skip any video_id already processed (YT:{video_id}: prefix)
   → fetch_youtube_transcript() on unprocessed videos
   → Extract concrete rules: session windows, sweep criteria, displacement thresholds

2. WEB CONTEXT
   web_search("gold futures ICT setup today")
   web_search("dollar index liquidity levels [date]")
   → Grab macro context: key levels, session biases, news events

3. BACKTEST
   → run_judas_threshold_sweep() / run_walk_forward() / run_custom_backtest()
   → Test extracted ideas against cached 1H bars for all relevant symbols
   → Target: 2-5 backtest runs per session

4. PROPOSE OR DISCARD
   → PF > 1.5 and ≥ 20 trades: propose_candidate()
   → Failed: record_finding() with "REJECTED: [reason]" — never re-tested
   → record_finding() with video_id as dedup key regardless of outcome

5. MULTI-SYMBOL SWEEP
   → Promising parameter set → sweep all 8 symbols in same session
   → Symbols: MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J
   → One backtest per symbol (pure Python loops inside the tool — no extra LLM calls)
```

### Researcher tool palette (20 action tools)

Discovery data is pre-loaded, not fetched via tools. Every tool call does real work:

| Category | Tools |
|---|---|
| Ingestion | `search_youtube_trading_videos`, `fetch_youtube_transcript`, `web_search`, `web_fetch` |
| File access | `read_file`, `list_files`, `read_research_artifact` |
| Backtesting | `run_judas_threshold_sweep`, `run_walk_forward`, `run_custom_backtest` |
| Proposals | `propose_candidate`, `propose_custom_strategy` |
| Task queue | `claim_task`, `complete_task` |
| Memory | `record_finding`, `retract_finding` |
| DB/detail | `get_strategy_detail`, `get_strategy_dossier`, `query_db`, `get_recent_pnl` |

---

## Agent Cadences (V2 — see `AGENTIC_PLAN_V2.md`)

| Service | Cadence | LLM Requests/Week | Purpose |
|---|---|---|---|
| `judas-crew.timer` | Hourly | **0** | Deterministic scanner — places brackets, reconciles trades |
| `judas-researcher.timer` | Every 90 min (13:30–21:00 UTC) + 22:00 + 04:00 UTC | **18,000–22,000** | YouTube → backtest → propose |
| `judas-operator.timer` | 2×/day (06:00 + 21:00 UTC) | **3,000–4,000** | Portfolio review, delegations, daily brief |
| `judas-registrar.timer` | 3×/day (06:30, 13:30, 21:30 UTC) | **< 200** | Queue flush: promote/retire only |
| `judas-trader.timer` | Hourly self-gate | **< 100** | Execute trader tasks (skips when idle) |
| `judas-coder.timer` | Hourly self-gate | **< 100** | Execute coder tasks (skips when idle) |
| `judas-dashboard.service` | Always-on | 0 | Flask + frontend on `:5080` |

---

## Promotion / Demotion Pipeline

**Researcher proposes → Operator approves → Registrar executes**

1. Researcher runs backtest → PF > 1.5 → calls `propose_candidate()` → row in `strategy_candidates`
2. Operator (2×/day) reviews candidates queue, delegates approve/reject to Registrar
3. Registrar calls `promote_candidate(id)` → atomically retires prior version, inserts v+1 active row

**Demotion:**
1. Operator checks live metrics (rolling 20-trade PF, days since last fire, expectancy)
2. Delegates retire decision to Registrar
3. Registrar calls `retire_strategy(id, reason)` → full audit trail in `auto_demotions`

---

## Authority Envelope

| Capability | Allowed | Forbidden |
|---|---|---|
| Account mode | `paper` only — hard-locked, raises on anything else | live / real money |
| Instruments | MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J | unrecognized symbols |
| Sleeve cap | $5,000 paper; kill switch at 25% drawdown | exceeding sleeve |
| Code-write | Coder agent on isolated branches; never directly to master | direct master push from autofix |
| Order-path files | Write-protected from autofix: `ibkr_executor.py`, `ibkr_data.py`, `config.py`, `config.yaml`, `src/risk/**` | autofix touching these |
| Cross-repo | Workshop is read-only baseline | mutating workshop |

### Safety rails

1. `mode='paper'` check raises at construction — no live orders possible
2. `kill.flag` in repo root halts trading on next tick
3. `autofix.disable` flag halts the autofix path
4. `max_open_positions` cap enforced deterministically in `_gate_fire()`
5. Per-strategy `already_open` gate prevents stacking
6. Hourly reconcile against IBKR positions — halt new entries on mismatch
7. `auto_demotions` is append-only — full audit trail with one-click reactivate

---

## Accounts and Config

| Setting | Value |
|---|---|
| IBKR paper account | DUH860616 |
| IBKR port | 4002 |
| Data clientId | 150 |
| Exec clientId | 151 |
| LLM | MiniMax M2.7 via `minimax/MiniMax-M2.7` in litellm |
| DB | `judas_crew.db` (SQLite WAL mode) |
| Dashboard | `http://127.0.0.1:5080/` |

---

## Repo Layout

```
main.py                            Entry point (hourly scanner)
config.yaml                        Mode, IBKR, risk params
src/
  research/
    researcher_agent.py            Researcher — pre-load + 20-tool action schema
    operator_agent.py              Operator — delegations + daily brief
    registrar_agent.py             Registrar — atomic promote/retire/modify
    trader_agent.py                Trader — order execution from task queue
    coder_agent.py                 Coder — autofix harness consumer
    agent_runner.py                Shared ReAct loop for all agents
  portfolio_runtime.py             Hourly scanner (zero LLM — deterministic)
  strategy_registry.py             Atomic promote/retire/reactivate
  tools/                           All tool implementations
  db/models.py                     SQLite schema + init_db
systemd/
  judas-crew.{service,timer}       Hourly trading (zero LLM)
  judas-researcher.{service,timer} 90-min blitz + 2 nightly
  judas-operator.{service,timer}   2×/day 06:00+21:00 UTC
  judas-registrar.{service,timer}  3×/day queue-flush
  judas-trader.{service,timer}     Hourly self-gate
  judas-coder.{service,timer}      Hourly self-gate
  judas-dashboard.service          Always-on Flask + frontend on :5080
  install.sh                       Rootless --user install
knowledge_base/
  judas_concepts.md                ICT Judas Swing reference
  buffet.yaml                      Workshop top-PF strategies
dashboard/                         React + TypeScript + Tailwind frontend
AGENTIC_PLAN_V2.md                 Authoritative design spec — source of truth
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

# Install systemd units
bash systemd/install.sh
```

---

## Common Operations

### Status check

```bash
# All agent timers
systemctl --user list-units "judas-*" --all

# Researcher last run
journalctl --user -u judas-researcher.service -n 50 --no-pager

# Turn counts across all agents today
journalctl --user -u judas-researcher.service -u judas-registrar.service \
  -u judas-operator.service -u judas-coder.service -u judas-trader.service \
  --since today --no-pager | grep -E "success=|turns="
```

### DB quick checks

```bash
# Task queue
sqlite3 judas_crew.db "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status"

# Recent trades
sqlite3 judas_crew.db "SELECT id, symbol, direction, pnl_dollars, status FROM trades ORDER BY opened_at DESC LIMIT 10"

# Active strategies
sqlite3 judas_crew.db "SELECT symbol, strategy_family, version, state FROM active_strategies WHERE state='active'"

# Pending candidates
sqlite3 judas_crew.db "SELECT id, symbol, strategy_family, backtest_pf, status FROM strategy_candidates ORDER BY created_at DESC LIMIT 10"

# Recent findings (YouTube dedup)
sqlite3 judas_crew.db "SELECT title, substr(body, 1, 100) FROM findings ORDER BY created_at DESC LIMIT 10"
```

### Manual triggers

```bash
systemctl --user start judas-researcher.service   # run one researcher session now
systemctl --user start judas-operator.service     # run operator review now
systemctl --user start judas-registrar.service    # flush registrar queue now
systemctl --user start judas-crew.service         # run one trading scan now
```

### Halt controls

```bash
touch kill.flag          # halt trading on next scanner tick
touch autofix.disable    # halt coder autofix path only
```

### Dashboard

- Local: `http://127.0.0.1:5080/`
- Tailnet: `http://omen-claw.tail76e7df.ts.net:5080/`

---

## Agent Gate Alignment (2026-05-27 overhaul)

### The core rule: empty slot beats net-loser

A symbol with zero strategies is **better** than a symbol with a strategy that fires losing trades. The agents were previously keeping bad strategies to "maintain coverage." That exception was removed.

### Promotion gates (all three required)

| Gate | Researcher acceptance | Reviewer pass | Registrar promote |
|---|---|---|---|
| 1 | PF > 1.5 | PF ≥ 1.3 | PF ≥ 1.3 |
| 2 | ≥ 20 trades | ≥ 20 trades | ≥ 20 trades |
| 3 | E[R] > 0 | E[R] > 0 | E[R] > 0 |
| 4 | Workshop fire check | — | — |

### Burnout rule

If a symbol has had 3+ auto-demotions in 7 days, the researcher skips re-trying the same family. Burnout summary is injected into every agent kickoff.

### Staleness grace period

A strategy active for fewer than 14 days is exempt from staleness-based retirement. The reviewer cannot retire a brand-new strategy just because it hasn't fired yet.

### Duplicate fingerprint

`_param_fingerprint()` hashes: engine + strategy_type + all numeric params. The old version used `displacement_r` (a key that doesn't exist in current params), so the fingerprint was always empty and every proposal looked unique.

---

## Sister Repo

`../judas-futures-workshop` — the deterministic Python workshop. Read-only baseline. Has its own systemd units (`judasfutures-*`) and DB (`judasfutures.db`). Uses clientIds 137/138; this repo uses 150/151. No imports across repos.

---

## Drift Prevention

`AGENTIC_PLAN_V2.md` is the authoritative design spec. Any architectural change that isn't reflected there is incomplete.
