# judas-agentic-crew — Implementation Plan

## Goal

A fully autonomous CrewAI trading system that applies ICT Judas Swing concepts to
micro futures via IBKR paper account. Completely separate from `judas-futures-workshop`
(different clientIds, different DB, different systemd services, no shared code imports).

---

## Key Decisions (confirmed with Rob)

| Decision | Choice |
|---|---|
| LLM | MiniMax M2.1 (primary) / M2.7 (heavy reasoning) via `https://api.minimax.io/v1` |
| Autonomy | Fully autonomous — LLM agents in the decision loop, deterministic tools for detection |
| Execution | IBKR paper only (`DUH860616`, hard-locked in code) |
| Timeframe | 1H bars (5m Judas is a documented loser — see Knowledge Base) |
| Knowledge base | Pre-loaded with RESEARCH_FINDINGS — agents know 5m is noise-killed |
| Output | Real IBKR paper orders + own SQLite DB tracking |

---

## Architecture

### Crew Flow (triggered by systemd timer, once per hour during market hours)

```
systemd timer
     │
     ▼
main.py --symbol MGC (or MNQ)
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│                      JudasCrew (sequential)                  │
│                                                              │
│  Task 1: Market Analysis                                     │
│  Agent: MarketAnalyst (MiniMax M2.1)                         │
│  Tools: ibkr_data_tool → fetch 100 x 1H bars                │
│         judas_detector_tool → sweep+CHoCH on 1H bars        │
│  Output: structured market summary + detected pattern (if any│
│                                                              │
│  Task 2: Setup Evaluation                                    │
│  Agent: SetupEvaluator (MiniMax M2.1)                        │
│  Tools: none (reasons from Task 1 output)                    │
│  Output: JSON {score: 0-10, rationale, trade_params or null} │
│                                                              │
│  Task 3: Risk Decision                                       │
│  Agent: RiskGuardian (MiniMax M2.1)                          │
│  Tools: db_daily_pnl_tool, db_open_positions_tool            │
│  Output: JSON {decision: TRADE|SKIP, reasoning}              │
│                                                              │
│  Task 4: Execution                                           │
│  Agent: TradeExecutor (MiniMax M2.1)                         │
│  Tools: ibkr_executor_tool, db_save_signal_tool,            │
│         db_save_trade_tool                                   │
│  Output: trade confirmation or skip log                      │
└─────────────────────────────────────────────────────────────┘
```

### Design Principle: LLM decides, deterministic code acts

- LLM agents read market data, reason about setup quality, decide to trade
- IBKR data fetching and order placement are deterministic tools (no LLM hallucination in order routing)
- Judas sweep+CHoCH detection is a deterministic tool — agents interpret the output, not re-derive it

### judas_detector Output Format (rich JSON)

```json
{
  "pattern_found": true,
  "direction": "short",
  "sweep": {
    "type": "wick",           // "wick" | "body" — wick sweeps are cleaner Judas setups
    "bar_idx": 94,
    "extreme_price": 3225.40,
    "prior_level_swept": "high",
    "prior_level_price": 3224.80
  },
  "choch": {
    "bar_idx": 97,
    "broken_pivot": 3218.60,
    "entry_price": 3218.20    // CHoCH bar close
  },
  "displacement": {
    "strength": 1.8,          // ratio vs 20-bar avg candle size; ≥1.5 = valid
    "atr_ratio": 0.94         // CHoCH bar range / 20-bar ATR
  },
  "fvg": {
    "present": true,          // Fair Value Gap in displacement move?
    "gap_high": 3221.50,
    "gap_low": 3220.10
  },
  "atr_context": {
    "current_atr": 4.20,
    "avg_atr_20": 5.10,
    "contracted": false       // true if current < 0.5× avg → RiskGuardian skips
  },
  "stop_price": 3225.80,     // sweep_extreme + 2 ticks buffer
  "target_price": 3211.60,   // entry - 2.0R
  "risk_dollars": 76.00      // based on MGC $10/point
}
```

SetupEvaluator uses `sweep.type`, `displacement.strength`, `fvg.present` as scoring inputs.
RiskGuardian uses `atr_context.contracted` as a hard gate.

---

## Folder Structure

```
judas-agentic-crew/
├── PLAN.md                          ← this file
├── CLAUDE.md                        ← orientation for Claude Code
├── README.md
├── main.py                          ← entry point: parse args, run crew
├── config.yaml                      ← symbols, clientIds, risk params
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .gitignore
│
├── src/
│   ├── config.py                    ← load config.yaml + env; mode guard
│   ├── logging_setup.py
│   │
│   ├── agents/
│   │   └── judas_agents.py          ← 4 CrewAI Agent definitions
│   │
│   ├── tasks/
│   │   └── judas_tasks.py           ← 4 CrewAI Task definitions
│   │
│   ├── tools/
│   │   ├── ibkr_data.py             ← fetch 1H bars (clientId=150)
│   │   ├── ibkr_executor.py         ← place paper orders (clientId=151)
│   │   ├── judas_detector.py        ← sweep+CHoCH detection; rich JSON output (see below)
│   │   └── db_tools.py              ← CrewAI tool wrappers around SQLite
│   │
│   ├── crews/
│   │   └── judas_crew.py            ← assembles agents + tasks into Crew
│   │
│   └── db/
│       └── models.py                ← SQLite schema + init_db + get_conn
│
├── knowledge_base/
│   ├── judas_concepts.md            ← ICT Judas Swing reference (agents load this)
│   └── research_findings.md         ← Workshop findings: 5m=loser, 1H=edge
│
├── outputs/
│   ├── signals/                     ← JSON signal logs per run
│   └── reports/                     ← periodic performance reports
│
├── logs/
│   └── .gitkeep
│
└── systemd/
    ├── judas-crew.service           ← oneshot: runs crew once
    ├── judas-crew.timer             ← hourly during NY + London sessions
    └── install.sh
```

---

## IBKR Integration

| Parameter | Value |
|---|---|
| Account | DUH860616 (same paper account as workshop) |
| Data clientId | 150 |
| Exec clientId | 151 |
| Workshop uses | 137 (engine), 138 (dashboard) — no conflict |
| Port | 4002 |
| Mode lock | `ValueError` if `config.yaml mode != "paper"` |

---

## Symbols (initial)

| Symbol | Tick | $/point | IBKR ticker |
|---|---|---|---|
| MGC | $0.10 | $10 | MGC |
| MNQ | $0.25 | $2 | MNQ |

Both on 1H bars. Prior session levels = prior trading day's high/low.

---

## Database Schema (`judas_crew.db`)

### `signals` table
Every setup the crew evaluates (whether traded or skipped).

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| ts_utc | TEXT | ISO timestamp of the detected setup |
| symbol | TEXT | MGC / MNQ |
| direction | TEXT | long / short |
| quality_score | INTEGER | 0-10 from SetupEvaluator |
| risk_decision | TEXT | TRADE / SKIP |
| entry | REAL | |
| stop | REAL | |
| target | REAL | |
| rationale | TEXT | LLM reasoning |
| agent_notes | TEXT | full agent output JSON |
| raw_llm_output | TEXT | raw CrewAI task outputs (for debugging/replay) |
| created_at | TEXT | row insert time |

### `trades` table
Every order actually placed.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| signal_id | INTEGER | FK → signals.id |
| ibkr_order_id | INTEGER | |
| symbol | TEXT | |
| direction | TEXT | |
| qty | INTEGER | |
| entry_fill | REAL | actual fill price |
| stop_price | REAL | |
| target_price | REAL | |
| exit_fill | REAL | null until closed |
| pnl_dollars | REAL | null until closed |
| status | TEXT | open / closed / cancelled |
| opened_at | TEXT | |
| closed_at | TEXT | |

---

## MiniMax LLM Config

```python
from crewai import LLM

llm = LLM(
    model="minimax/MiniMax-M2.1",       # minimax/ prefix → LiteLLM native MiniMax support
    api_key=os.environ["MINIMAX_API_KEY"],
    api_base="https://api.minimax.io/v1",
    temperature=0.0,
    max_tokens=4096,
)
```

**CONFIRMED WORKING** (2026-05-08 probe): `finish_reason=tool_calls`, proper `function.name`
+ `function.arguments` emitted. LiteLLM strips `<think>` blocks automatically.

Available on this plan: `MiniMax-M2`, `MiniMax-M2.1`, `MiniMax-M2.7`  
Highspeed variants not on this plan.  
Use `minimax/` prefix (not `openai/`) — LiteLLM has native MiniMax tool-call normalization.

---

## Agent Definitions

### 1. MarketAnalyst
- **Role**: Futures market data analyst
- **Goal**: Fetch current 1H bars for the target symbol, detect Judas sweep+CHoCH pattern
- **Backstory**: Expert in reading futures market structure. Knows that 5m Judas has
  negative expectancy (-0.21R on MGC). Works exclusively on 1H bars. Runs the
  deterministic sweep+CHoCH detector and interprets the raw output.
- **Tools**: ibkr_data_tool, judas_detector_tool

### 2. SetupEvaluator
- **Role**: ICT setup quality scorer
- **Goal**: Score the detected setup 0-10 using deep ICT/Judas knowledge
- **Backstory**: Master of ICT concepts. Knows that a textbook Judas requires: (1) clean
  wick sweep of prior session high/low — NOT a close through, (2) displacement candle
  >1.5x average size, (3) CHoCH breaking a clear structural swing. Applies "best setups
  only" discipline. Score ≥ 7 is high quality, 5-6 is marginal (will likely be rejected),
  < 5 is a skip. If no pattern detected, score is 0.
- **Tools**: none (reasons from MarketAnalyst output)

### 3. RiskGuardian
- **Role**: Strict risk gatekeeper
- **Goal**: Output TRADE or SKIP. Never approve a marginal setup.
- **Backstory**: Extremely disciplined. Checks: daily loss limit ($300), max open positions
  (2), session validity (NY or London only), setup quality score (≥ 6 required), ATR
  contraction filter (rejects setups when current ATR < 0.5× 20-bar average — no edge in
  compressed ranges). Patient — would rather miss 10 real setups than take 1 bad one.
  If in doubt: SKIP.
- **Tools**: db_daily_pnl_tool, db_open_positions_tool
- **Note**: No NY-open lockout. NY 9:30–10:30 ET IS the prime Judas window — the sweep
  often happens in the first 30 minutes. A lockout here would skip the best setups.

### 4. TradeExecutor
- **Role**: IBKR paper order executor and trade recorder
- **Goal**: If risk decision is TRADE, place the paper order and record everything to DB
- **Backstory**: Precise and disciplined executor. Places orders exactly as specified —
  no deviations. Records every signal (even skips) to the DB for performance tracking.
  Confirms order placement before closing the run.
- **Tools**: ibkr_executor_tool, db_save_signal_tool, db_save_trade_tool

---

## Knowledge Base Content

### `knowledge_base/judas_concepts.md`
- What is a Judas Swing (false move to sweep liquidity before the real move)
- Session liquidity windows: Asia builds the pool, London sweeps it, NY confirms
- Equal highs/lows as liquidity magnets
- Sweep anatomy: approach → wick through → close back inside
- Displacement: impulsive reversal move with big candles
- FVG (Fair Value Gap), Order Block (OB) — refinement entry tools
- CHoCH vs BOS
- Entry: on CHoCH bar close
- Stop: 2-3 ticks beyond sweep extreme
- Target: 2R minimum, or next HTF liquidity pool
- "Best setups only" criteria (all must be met)

### `knowledge_base/research_findings.md`
- **5m Judas is a documented loser**: -$93 net, 19 trades, 26% WR, -0.21R expectancy on MGC
- **Root cause**: 5m is too noisy; stops eaten before the move develops
- **1H edge**: 40/81 strategy combos profitable on NQ 1H (vs 2/81 on 5m)
- **Best 1H combo**: RSI 25/75 mean-revert, 2.0R, 1.5×ATR stop → +$10,677 / 43% WR / +0.30R
- **Working hypothesis**: Judas logic on 1H bars should outperform 5m; this crew will validate

---

## Systemd Schedule

Timer fires hourly at :00 during:
- London session: 03:00–08:00 UTC
- NY session: 14:00–21:00 UTC

On each fire: crew runs once for each configured symbol, evaluates current bar.

---

## Risk Rules (enforced by RiskGuardian)

| Rule | Value |
|---|---|
| Daily loss limit | -$300 |
| Max open positions | 2 |
| Max contracts per trade | 1 |
| Session gate | NY or London only (no NY-open lockout — 9:30–10:30 ET is prime Judas time) |
| Min quality score | 6/10 |
| ATR contraction filter | Skip if current ATR < 0.5× 20-bar average ATR |
| Patience rule | If any doubt → SKIP |

---

## Implementation Order

1. [ ] Repo structure + pyproject.toml + config.yaml + .env.example
2. [ ] `src/config.py` — config loader with mode guard
3. [ ] `src/db/models.py` — SQLite schema + init
4. [ ] `src/tools/judas_detector.py` — port sweep+CHoCH from workshop
5. [ ] `src/tools/ibkr_data.py` — 1H bar fetcher (clientId=150)
6. [ ] `src/tools/ibkr_executor.py` — paper order placement (clientId=151)
7. [ ] `src/tools/db_tools.py` — CrewAI tool wrappers
8. [ ] `knowledge_base/` — both markdown files
9. [ ] `src/agents/judas_agents.py` — 4 agent definitions
10. [ ] `src/tasks/judas_tasks.py` — 4 task definitions
11. [ ] `src/crews/judas_crew.py` — crew assembly
12. [ ] `main.py` — entry point
13. [ ] `systemd/` — service + timer + install.sh
14. [ ] `CLAUDE.md` — orientation for future sessions

---

## Phase 1 vs Phase 2

### Phase 1 (this build) — Score + Execute
The crew is a disciplined scorer and executor. It reads structured tool output,
reasons about setup quality, and places paper orders. It does NOT self-modify
or discover new parameters. This is the honest description of what Phase 1 does.

### Phase 2 (deferred — needs real trade data)
- **IterationAgent**: reviews closed trade history, identifies systematic patterns
  in what RiskGuardian skipped vs what would have been profitable. Suggests parameter
  adjustments (quality thresholds, ATR filter multiplier). Requires ≥30 closed trades.
- **MNQ support**: second symbol, once MGC is validated. Separate clientId range.
- **Self-tuning**: IterationAgent adjusts RiskGuardian thresholds based on live results.

---

## Resolved Design Decisions

| Question | Decision |
|---|---|
| Phase 1 symbols | **MGC only** — validate the stack before adding MNQ |
| Multi-symbol dispatch | One service call per symbol; systemd `ExecStart` loops over symbols list |
| Fill tracking | **Phase 1**: mark trade `status=open`, reconcile manually via IBKR TWS. A dedicated `judas-crew-fill-sync.service` (mirrors workshop pattern) is Phase 2 work. |
| "2 parallel candidates" | **Rejected** — sweep is one direction per bar (high OR low). Two candidates would be theater. |
| NY open lockout | **Rejected** — NY 9:30–10:30 ET is the prime Judas window. No lockout. |
