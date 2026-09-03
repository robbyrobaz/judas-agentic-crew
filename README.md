# Judas Agentic Crew

An autonomous futures-research and simulated-execution system. A deterministic
Python scanner evaluates registered strategies and places bracket orders through
NinjaTrader, while specialist LLM agents research, review, promote, retire, and
repair strategies around it. The order path itself makes no LLM calls.

> **Current venue:** NinjaTrader simulation account `SimJudasFutures`.
> Nothing in the current dashboard, trade ledger, or account P&L should be
> described as live or real-money performance.

## Current snapshot — 2026-09-02

| Component | Current state |
|---|---|
| Execution | NinjaTrader SIM, account `SimJudasFutures` |
| Market data | IBKR only; the project never subscribes through NinjaTrader ATI |
| Scanner | Deterministic, every 5 minutes while Globex/session gates allow |
| Position/account guard | Every minute via `judas-crew-reconciler.timer` |
| Active registry | 25 configurations: 3× 6J, 11× MGC, 9× MNQ, 2× ZF |
| Research history | 8,281 experiments and 6,908 candidates in the local SQLite database |
| Registry history | 4,445 versions: 25 active, 4,364 retired, 56 superseded |
| Dashboard | `http://127.0.0.1:5080/` |

The database is runtime state and is intentionally not committed. See
[`docs/STRATEGY_SUMMARY.md`](docs/STRATEGY_SUMMARY.md) for a dated, reviewable
snapshot of every active configuration and the best strategy families found.
The corresponding private data-backup inventory and checksums are recorded in
[`docs/BACKTEST_DATA_BACKUP.md`](docs/BACKTEST_DATA_BACKUP.md).

## Architecture

```text
IBKR market data ──► multi-timeframe bar cache ──► deterministic scanner
                                                       │
                                                       ▼
                                            NinjaTrader SIM brackets
                                                       │
                              position/order/account truth via ATI + files
                                                       │
                 1-minute reconciler ◄─────────────────┘
                        │
                        ├─ account P&L guard
                        └─ orphan/protection tasks ──► specialist agents

researcher ─► candidate ─► operator/reviewer ─► registrar ─► active registry
                                      coder handles bounded autofix tasks
```

NinjaTrader is used for orders, positions, working-order state, and account
summary only. Price subscriptions are sourced from IBKR. Contract selection is
resolved from IBKR volume and written to `active_contracts.json`; order placement
fails closed if that resolution is missing or stale.

## Strategy portfolio

The active portfolio is concentrated in three researched architectures:

- **ATR/displacement continuation** — the strongest repeatable cross-symbol
  result. Active on 6J 5m, MGC 5m/15m, MNQ 5m, and ZF 15m, with multiple
  displacement thresholds and directional filters.
- **Silver Bullet / rolling PDH-PDL retest** — active on MGC and MNQ at 5m/15m.
  Moderate-PF variants are promising; very high PF or 80%+ win-rate variants
  remain overfit-watch items until enough sim fills accumulate.
- **iFVG midpoint reversion with HTF bias** — active on MGC and MNQ 15m, with
  recorded PF around 2.1–2.5 and useful sample sizes.

The portfolio also includes MNQ CISD/FVG and MGC range-cycle displacement
experiments. Backtest figures are evidence, not guarantees. Older registry
metrics were produced before the current `v1_realistic_micros` cost stamp and
must be revalidated before any real-money use.

Full metrics, evidence tiers, caveats, and all 25 active rows are in the
[strategy summary](docs/STRATEGY_SUMMARY.md).

## Deterministic risk controls

Source of truth: [`src/research/lucid_guard.py`](src/research/lucid_guard.py).
The Lucid-style limits remain enabled in sim so the same safety behavior can be
tested without risking capital.

| Guard | Rule | Action |
|---|---|---|
| Daily profit soft | +$1,200 including unrealized | Halt new entries and latch for the trading day |
| Daily profit hard | +$1,500 including unrealized | `CLOSEPOSITION` all instruments and latch for the day |
| Daily loss | Cushion-scaled; ceilings −$700 soft / −$900 hard | Halt or flatten before the trailing limit |
| Trailing MLL | $2,000 from peak daily-close equity | Flatten and halt |
| Aggregate contracts | 10 across the account | Reject new entries |
| EOD | Flatten at 16:40 ET | Flat before the 16:45 cutoff |
| Banned symbols | MET, MBT, DX | Reject entries |

Open P&L is marked from the freshest IBKR bar available. Missing account,
position, contract, or price data halts entries fail-closed. Existing strategy
TP/SL brackets remain broker-native; account-level hard exits use NinjaTrader
`CLOSEPOSITION`, which flattens and cancels working orders atomically.

## Agent workflow and cadence

| Unit | Cadence | Purpose |
|---|---|---|
| `judas-crew.timer` | every 5 min | Refresh bars, reconcile fills, evaluate strategies, place brackets |
| `judas-crew-reconciler.timer` | every 1 min | Account guard and NT-truth/orphan reconciliation |
| `judas-crew-watchdog.timer` | every 5 min | Detect and terminate a hung scanner |
| `judas-crew-ledger.timer` | hourly at :25 | Reconcile NT fills missing from the local ledger |
| `judas-reviewer.timer` | hourly | Review new evidence and registry mutations |
| `judas-trader.timer` | hourly at :05, plus urgent kicks | Resolve position/order tasks |
| `judas-coder.timer` | hourly at :10 | Process bounded autofix tasks |
| `judas-researcher.timer` | every 6 h | Research, backtest, and propose candidates |
| `judas-operator.timer` | every 6 h | Portfolio review and delegation |
| `judas-registrar.timer` | every 8 h | Apply approved promotions/retirements |
| `judas-dashboard.service` | continuous | Monitoring UI and API on port 5080 |

Agent loops have turn, wall-time, per-run token, and shared daily-token budgets.
The trader is exempt from the research token budget because position protection
must remain available.

## Safety invariants

- Scanner/order decisions are deterministic; LLMs cannot bypass risk gates.
- The current account must come from `config.yaml`; no stale account fallback.
- Timed-out entries are cancelled rather than left working.
- Every filled entry must receive both protective legs or be emergency-flattened.
- Orphan flattening uses `CLOSEPOSITION`, never a naked opposing market order.
- Contract rolls use the highest-volume eligible IBKR contract and fail closed.
- NinjaTrader ATI market-data subscriptions are prohibited.
- `kill.flag` halts scanner entries; `autofix.disable` halts coder mutations.
- Custom backtests use contract dollar economics, quantity, execution costs, and
  the `v1_realistic_micros` cost-model stamp.

Additional reliability rules are recorded in
[`knowledge_base/runtime_reliability_2026-09-02.md`](knowledge_base/runtime_reliability_2026-09-02.md).

## Setup

Requirements: Python 3.11+, an IBKR Gateway/TWS connection, NinjaTrader on the
configured Windows bridge host, and systemd user services for unattended use.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
# Fill credentials locally; .env is ignored by git.

.venv/bin/pytest -q

cd dashboard && npm ci && npm run build && cd ..

# Review config.yaml before enabling services.
./systemd/install.sh
```

Never commit `.env`, account credentials, the SQLite database, cache files, or
runtime state.

## Common operations

```bash
systemctl --user list-timers 'judas-*' --all
systemctl --user status judas-dashboard.service judas-crew.timer

cat data/lucid_guard_state.json
.venv/bin/python -c \
  "from src.research.agent_tools import get_nt_positions; print(get_nt_positions())"

sqlite3 judas_crew.db \
  "SELECT id,symbol,strategy_family,version FROM active_strategies WHERE state='active'"
sqlite3 judas_crew.db \
  "SELECT id,symbol,direction,pnl_dollars,exit_reason,closed_at FROM trades ORDER BY id DESC LIMIT 20"

journalctl --user -u judas-crew.service --since -30min --no-pager
journalctl --user -u judas-crew-reconciler.service --since -30min --no-pager

touch kill.flag
touch autofix.disable
```

## Repository map

```text
main.py                         scanner entry point
config.yaml                     route, account, symbols, sessions, limits
src/bar_cache.py                IBKR bars and active-contract resolution
src/portfolio_runtime.py        deterministic strategy and order runtime
src/broker/ninjatrader.py       NinjaTrader order/account/position bridge
src/research/lucid_guard.py     account-level risk policy
src/research/                   specialist agents, tools, and backtests
src/strategy_registry.py        promotion/retirement audit trail
scripts/position_reconciler.py  one-minute account and position guard
dashboard/                      Flask API and React UI
systemd/                        user service/timer definitions
tests/                          automated regression suite
docs/STRATEGY_SUMMARY.md        dated active-strategy evidence snapshot
docs/BACKTEST_DATA_BACKUP.md    private backtest-data backup inventory
```

## Historical context

The repository previously traded several Lucid evaluation accounts. Those eras
exposed daily-loss, trailing-drawdown, stale-account, orphan-order, and ledger
attribution failures. Their records remain in the local database for research,
but the current era began on `SimJudasFutures` on 2026-09-02. Historical P&L
must not be mixed with current sim performance.
