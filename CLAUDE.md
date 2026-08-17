# judas-agentic-crew — Orientation for Claude

> **⚠️ STALENESS WARNING (2026-08-16):** much of this file describes the Phase-1
> IBKR-paper CrewAI era. THE SYSTEM NO LONGER TRADES PAPER. Current reality:
> - **Execution: REAL Lucid 50K Flex eval via NinjaTrader** (account in
>   `config.yaml` `ninjatrader.account`; WinRM bridge to REBEL). Runtime is
>   `portfolio` (deterministic 5-min scan, zero LLM); the CrewAI flow below is legacy.
> - **LLM: MiniMax-M3** (not M2.1). Agents: researcher/operator/registrar/trader/coder/reviewer.
> - **ROB'S MANDATE:** pass the $3k eval FAST — eval risk budget = the ~$95 reset
>   fee. Micros (MNQ/MGC/MCL) use RISK-BASED sizing: $250 risk per trade, qty
>   clamped [strategy qty, 5]. Guards: daily loss soft -$700 / hard -$900 flatten
>   + minute backstop, $2k trailing MLL, 10-contract aggregate cap, EOD flat
>   16:40 ET — sized to Lucid's unannounced ~$1,200 eval DLL.
> - Risk rules below (RiskGuardian -$300 etc.) apply ONLY to the dormant legacy crew path.
> - Position truth: NT sqlite does NOT persist live Lucid positions — the scan and
>   1-min reconciler read NT's per-instrument position files. Ledger completeness is
>   enforced hourly by scripts/nt_ledger_gap_sync.py (nt_ledger_gap rows).
> - See README.md (rewritten 2026-08-13) for the current architecture.

## What This Repo Is

A fully autonomous CrewAI-based trading system that applies ICT Judas Swing concepts to
micro futures via IBKR paper account. Completely separate from `judas-futures-workshop`.

**Key distinctions from workshop:**

| | judas-agentic-crew | judas-futures-workshop |
|---|---|---|
| Approach | LLM agents in decision loop | Pure Python rule-based |
| clientId data | 150 | 137 |
| clientId exec | 151 | 138 |
| DB file | `judas_crew.db` | `judasfutures.db` |
| Timeframe | 1H bars | 5m bars (Judas), mixed |
| Systemd prefix | `judas-crew` | `judasfutures-` |
| Port | 4002 (same IBKR Gateway) | 4002 |

No imports from workshop — fully standalone. Copy any logic you need; don't import across repos.

## Live Install Facts

- **IBKR paper account**: DUH860616
- **Port**: 4002 (same IBKR Gateway as workshop — clientIds don't conflict)
- **Data clientId**: 150 (fetches 1H bars via ib_async)
- **Exec clientId**: 151 (places market orders via ib_async)
- **DB**: `judas_crew.db` in repo root (SQLite, WAL mode)
- **Logs**: `logs/judas_crew.log` (JSON structured, rotating 10MB x 5)
- **venv**: `.venv/` (Python 3.11, crewai 1.14.4, litellm)
- **LLM**: MiniMax M2.1 via `https://api.minimax.io/v1` (minimax/ prefix for LiteLLM)

## Where Things Are

```
main.py                    Entry point (--symbol MGC)
config.yaml                Config: mode, ibkr, risk params
.env                       MINIMAX_API_KEY (not committed)
src/
  config.py                Config loader + mode guard (ValueError if mode != paper)
  logging_setup.py         JSON structured logging with rotation
  agents/judas_agents.py   4 CrewAI Agent definitions
  tasks/judas_tasks.py     4 CrewAI Task definitions
  crews/judas_crew.py      Crew assembly (sequential process)
  tools/
    judas_detector.py      Sweep+CHoCH detector + @tool wrapper (judas_detector_tool)
    ibkr_data.py           1H bar fetcher (ibkr_data_tool)
    ibkr_executor.py       Paper order placement (ibkr_executor_tool)
    db_tools.py            SQLite wrappers: pnl, positions, save signal/trade
  db/
    models.py              SQLite schema: signals + trades tables, init_db, get_conn
knowledge_base/
  judas_concepts.md        ICT Judas Swing reference (loaded into agent knowledge)
  research_findings.md     5m=-loser/-0.21R, 1H=edge/+0.30R findings
systemd/
  judas-crew.service       Oneshot systemd service
  judas-crew.timer         Hourly timer (London 03-08 UTC, NY 14-21 UTC)
  install.sh               Copy + enable timer (run once to install)
```

## Crew Flow

```
main.py --symbol MGC
  → init_db()
  → JudasCrew(symbol="MGC").kickoff()
      Task 1: MarketAnalyst → ibkr_data_tool → judas_detector_tool
      Task 2: SetupEvaluator → scores 0-10 (no tools, reasons from T1)
      Task 3: RiskGuardian → db_daily_pnl_tool + db_open_positions_tool → TRADE|SKIP
      Task 4: TradeExecutor → ibkr_executor_tool + db_save_signal_tool + db_save_trade_tool
```

## Systemd Commands

```bash
# Install timer (run once)
bash systemd/install.sh

# Check timer status
systemctl --user status judas-crew.timer
systemctl --user list-timers judas-crew.timer

# Fire manually (useful for testing during market hours)
systemctl --user start judas-crew.service

# View logs
journalctl --user -u judas-crew.service -n 50 --no-pager

# Stop and disable
systemctl --user stop judas-crew.timer
systemctl --user disable judas-crew.timer
```

## Common Operations

```bash
# Test the full crew manually
cd /home/rob/judas-agentic-crew
.venv/bin/python main.py --symbol MGC --log-level DEBUG

# Initialize DB only
.venv/bin/python -c "from src.db.models import init_db; init_db('judas_crew.db')"

# Query signals
sqlite3 judas_crew.db "SELECT * FROM signals ORDER BY created_at DESC LIMIT 10"

# Query trades
sqlite3 judas_crew.db "SELECT * FROM trades ORDER BY opened_at DESC LIMIT 10"

# Today's P&L
sqlite3 judas_crew.db \
  "SELECT SUM(pnl_dollars), COUNT(*) FROM trades WHERE closed_at LIKE '$(date +%Y-%m-%d)%' AND status='closed'"
```

## Risk Rules (enforced by RiskGuardian agent)

| Rule | Value |
|---|---|
| Daily loss limit | -$300 |
| Max open positions | 2 |
| Max contracts per trade | 1 |
| Min quality score | 6/10 |
| ATR contraction gate | Skip if current_atr < 0.5 × avg_atr_20 |
| Patience rule | Any doubt → SKIP |
| NY-open lockout | NONE — 09:30-10:30 ET is the prime Judas window |

## Phase 1 vs Phase 2 Scope

**Phase 1 (current — validate the stack):**
- MGC only
- Score + execute (no self-modification)
- Fill tracking: trades marked `status=open`, reconcile manually via IBKR TWS
- Goal: ≥30 closed trades, WR ≥35%, expectancy ≥0R

**Phase 2 (deferred — needs real trade data):**
- IterationAgent: reviews closed trades, suggests threshold adjustments
- Fill sync service: auto-reconcile fills from IBKR
- MNQ support: second symbol once MGC validated
- Self-tuning: IterationAgent adjusts quality thresholds from live results

## Adding a New Symbol

1. Add to `config.yaml` under `symbols:` with tick, dollar_per_point, timeframe
2. Add to `_CONTRACT_SPECS` in both `ibkr_data.py` and `ibkr_executor.py`
3. Add to `_tick_map` and `_dpp_map` in `judas_detector.py`
4. Add a `judas-crew-MNQ.service` (copy + change --symbol arg)
5. Update systemd timer's `Unit=` to run both, or create a separate timer

## Important Notes

- **Paper only hard lock**: `ibkr_executor.py` checks config.yaml mode at runtime.
  Any `mode != "paper"` raises ValueError and blocks execution.
- **clientId separation**: 150=data, 151=exec. Workshop uses 137/138. No conflicts.
- **No shared imports**: Never import from judas-futures-workshop. Copy needed logic.
- **DB separation**: judas_crew.db ≠ judasfutures.db. Never query across DBs.
- **LLM prefix**: Use `minimax/MiniMax-M2.1` (not `openai/`). LiteLLM has native
  MiniMax support under the `minimax/` prefix which handles tool-call normalization.
