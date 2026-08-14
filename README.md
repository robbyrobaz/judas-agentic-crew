# judas-agentic-crew

A self-running futures trading system: a **pure-deterministic Python scanner** places
bracket orders on a **real-money Lucid 50K eval account** via NinjaTrader, while a crew of
**MiniMax-M3 LLM agents** (researcher, operator, registrar, trader, coder, reviewer)
continuously researches strategies, reviews the portfolio, and manages anything the
deterministic layer flags. The scanner that touches money makes **zero LLM calls**; the
LLM budget is spent entirely on research and judgment.

---

## Current State (2026-08-13)

| | |
|---|---|
| **Execution** | REAL LucidFlex 50K eval `LFE05064290360100` via NinjaTrader on REBEL (WinRM bridge, ATI order files) — wired 2026-08-12 |
| **Scanner** | Every 5 min whenever Globex is open — deterministic, 0 LLM calls |
| **LLM** | MiniMax-M3 (`https://api.minimax.io/v1` via LiteLLM) for all agents |
| **Active strategies** | 36 active (4,301 retired, 48 superseded — full audit trail in `active_strategies`) |
| **Symbols** | MGC, MNQ, ZF, MCL, 6J only (crypto MET/MBT + DX banned under Lucid rules) |
| **Dashboard** | `http://127.0.0.1:5080/` (tailnet: `omen-claw.tail76e7df.ts.net:5080`) |
| **Position watchdog** | `judas-crew-reconciler.timer` — every **1 minute**, NT truth vs DB (added 2026-08-13) |

### Account timeline

| Era | Account | Outcome |
|---|---|---|
| → 2026-07-24 | IBKR paper → NT `SimJudasCrew` | Sim proving: +$2,131 / 487 trades, P(pass)~70% Monte-Carlo |
| 2026-07-26 → 07-30 | **LFE..89** (real 50K eval) | −$849 / 59 trades. **DIED 07-30**: the −$1,198 day on 07-29 tripped Lucid's *unannounced* ~$1,200 daily loss limit; account vanished from the feed |
| 2026-07-30 → 08-12 | (zombie period) | Every order rejected `account does not exist`; NT-reject auto-block froze the whole book — nobody noticed for 13 days |
| 2026-08-12 → | **LFE..100** (real 50K eval) | Current. Daily-loss guards added specifically so the DLL death can't repeat |

---

## Risk Guards (`src/research/lucid_guard.py` — enforced deterministically every scan)

| Guard | Value | Action |
|---|---|---|
| Daily loss **soft** | −$800 | halt new entries |
| Daily loss **hard** | −$1,000 | flatten all + halt (sits under Lucid's unannounced ~$1,200 eval DLL) |
| Daily profit soft | +$1,200 | halt new entries |
| Daily profit hard | +$1,500 | flatten all + halt (consistency cap = 50% of $3k target) |
| Trailing MLL | $2,000 from peak daily-close equity | flatten all + halt — terminal |
| Aggregate contracts | ≤ 4 across the whole book (live NT count, not DB rows) | gate entries |
| EOD flat | flatten 16:40 ET (Lucid cutoff 16:45) | flatten all + halt |
| Banned symbols | MET, MBT, DX | entry gate refuses |

Day P&L includes **unrealized** (positions marked to latest bar close). Guards fail-open on
an NT-data outage; deterministic banned/EOD gates always apply.

---

## Position Truth & the 1-Minute Reconciler (2026-08-13)

Hard-won facts about where live positions actually live:

- **NT's sqlite `Positions` table does NOT persist live Tradovate/Lucid positions** — it
  sat empty while the account held 4 contracts. The live truth is the per-instrument
  position files NT writes to `outgoing/<contract> <exch>_<account>_position.txt`
  (`LONG;3;0.0062947`) on every position update. `NTBroker.positions()` merges both.
- All agent NT tools resolve the account from `config.yaml` — never a hardcoded default.
  (A stale `SimJudasCrew` default left the orphan check querying the flat sim account for
  18 days while real positions sat naked.)
- Entry orders that don't fill within the timeout are **cancelled**, not abandoned —
  abandoned entries filled hours later as naked unmanaged positions.

`scripts/position_reconciler.py` (every 1 min via `judas-crew-reconciler.timer`):
compares NT truth against open DB trades → any unmanaged contracts get a high-urgency
trader task (deduped) **and an immediate `judas-trader.service` start** — awareness gap is
~1 minute instead of the hourly tick. A 60-min same-book cooldown means a deliberate HOLD
by the trader isn't re-litigated every minute; any *change* in the unmanaged book fires
immediately. Per the 2026-07-17 mandate, the LLM crew decides hold/flatten — the
reconciler never auto-liquidates.

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │ NinjaTrader on REBEL (Windows, WinRM bridge)  │
                    │ Lucid/Tradovate feed → LFE..100 (real 50K)    │
                    └────────────▲──────────────────▲──────────────┘
             OIF order files    │                   │  position files / sqlite / trace
                                │                   │
  judas-crew.timer (5 min) ── portfolio_runtime.py ── lucid_guard.py   ← 0 LLM calls
                                │        │
                                │        └── _orphan_body() ──┐
  judas-crew-reconciler (1 min) ────────────────────────────── ┤→ agent_tasks (high urgency)
                                                               │       │
                                                               │       ▼
        MiniMax-M3 agents:  trader ◄── kicked immediately ─────┘   (hold / flatten / adopt)
        researcher · operator · registrar · reviewer · coder
```

## Agent Cadences (installed reality)

| Unit | Cadence | LLM | Purpose |
|---|---|---|---|
| `judas-crew.timer` | every 5 min (Globex open) | 0 | Deterministic scan: fire strategies, place brackets, guards, orphan detect |
| `judas-crew-reconciler.timer` | **every 1 min** | 0 | NT-truth position reconcile → task + immediate trader kick |
| `judas-crew-watchdog.timer` | every 5 min | 0 | Kills a hung scanner |
| `judas-trader.timer` | hourly self-gate (+ kicked on orphans) | low | Executes trader tasks (reconcile/close/hold) |
| `judas-reviewer.timer` | hourly | med | Registry mutations review |
| `judas-researcher.timer` | every 6 h | high | Research → backtest → propose (trimmed 07-09 after quota blowout) |
| `judas-operator.timer` | every 6 h | med | Portfolio review, delegations, daily brief |
| `judas-registrar.timer` | every 8 h (:40 offset) | low | Queue flush: promote/retire only |
| `judas-coder.timer` | hourly self-gate | low | Autofix tasks |
| `judas-dashboard.service` | always-on | 0 | Flask + React on `:5080` |

---

## Promotion / Demotion Pipeline

**Researcher proposes → Operator approves → Registrar executes**

1. Researcher backtests → PF gate → `propose_candidate()` → `strategy_candidates`
2. Operator reviews the queue, delegates approve/reject to Registrar
3. Registrar `promote_candidate(id)` → atomically retires prior version, inserts v+1
4. Demotion mirrors it: Operator flags on live metrics → Registrar `retire_strategy(id, reason)` → `auto_demotions` audit trail (append-only, one-click reactivate)

Core rule: **empty slot beats net-loser.**

---

## Safety Rails

1. `lucid_guard` — the full table above, evaluated deterministically every scan
2. `kill.flag` in repo root halts trading on next tick; `autofix.disable` halts the coder path
3. Aggregate 4-contract cap counts LIVE NT contracts + this scan's placements (not DB rows)
4. Per-strategy `already_open` gate prevents stacking
5. Entry timeout ⇒ entry order **cancelled** (no abandoned working orders)
6. Protective-leg death ⇒ `NAKED_RISK` emergency flatten of that fill
7. NT-reject auto-block: a symbol NT keeps rejecting is skipped on a 6 h cooldown
8. 1-min reconciler + orphan task path (see above) — unmanaged positions surface in ~1 min
9. Order-path files write-protected from autofix: broker, config, `src/risk/**`

Known operational trap: `flatten_position` on an orphan can **trigger stale entry-side
stops and double the orphan** (2026-07-23 incident). For orphans with no protective stop,
use `close_nt_position` — NT's CLOSEPOSITION flattens *and* cancels that instrument's
working orders atomically.

---

## Config

| Setting | Value |
|---|---|
| Route | `execution.route: ninjatrader` (`config.yaml`) |
| Account | `ninjatrader.account: LFE05064290360100` |
| NT host | `100.108.151.36` (Tailscale, REBEL), user `nqtrader`, password via `WINDOWS_PASSWORD` in `.env` |
| Bridge python | `C:\PyBridge\python.exe` |
| OIF dir | `C:\Users\hartw\Documents\NinjaTrader 8\outgoing` |
| LLM | `MiniMax-M3` @ `https://api.minimax.io/v1` (key in `.env`) |
| DB | `judas_crew.db` (SQLite WAL) |

---

## Repo Layout

```
main.py                            Entry point (scanner run)
config.yaml                        Route, NT account/bridge, risk params
scripts/
  position_reconciler.py           1-min NT-truth vs DB reconcile + trader kick
  run_dashboard.py                 Dashboard entry
src/
  portfolio_runtime.py             The 5-min scan (zero LLM): fires, gates, guards, orphan detect
  broker/ninjatrader.py            WinRM bridge: brackets, positions (sqlite+files), working orders, cancels
  research/
    lucid_guard.py                 LucidFlex eval rule guards (pure, tested)
    agent_tools.py                 LLM tool implementations (NT reads/cancels/close, tasks, backtests)
    operator_agent.py / researcher_agent.py / registrar_agent.py /
    trader_agent.py / coder_agent.py / reviewer via agent_runner.py
  strategy_registry.py             Atomic promote/retire/reactivate
  db/models.py                     SQLite schema
systemd/                           All unit files + install.sh (rootless --user)
dashboard/                         React + TypeScript + Tailwind frontend
knowledge_base/                    ICT Judas concepts, workshop baselines
AGENTIC_PLAN_V2.md                 Design spec
```

---

## Common Operations

```bash
# All crew units
systemctl --user list-units "judas-*" --all

# One scan / one trader run now
systemctl --user start judas-crew.service
systemctl --user start judas-trader.service

# Live guard snapshot (written every scan)
cat data/lucid_guard_state.json

# Open DB trades vs NT truth
sqlite3 judas_crew.db "SELECT symbol,direction,qty,entry_fill,opened_at FROM trades WHERE status='open'"
.venv/bin/python -c "from src.research.agent_tools import get_nt_positions; print(get_nt_positions())"

# Task queue / recent trades / active strategies
sqlite3 judas_crew.db "SELECT status, COUNT(*) FROM agent_tasks GROUP BY status"
sqlite3 judas_crew.db "SELECT id,symbol,direction,pnl_dollars,exit_reason,closed_at FROM trades ORDER BY id DESC LIMIT 10"
sqlite3 judas_crew.db "SELECT symbol,strategy_family,version FROM active_strategies WHERE state='active'"

# Reconciler health
journalctl --user -u judas-crew-reconciler.service --since -30min --no-pager | grep reconciler

# Halt controls
touch kill.flag          # halt trading on next scanner tick
touch autofix.disable    # halt coder autofix path only
```

---

## Lessons Encoded (why the code looks like this)

- **2026-07-17** — orphaned OCO legs opened positions with no DB row; fills-derived sync
  was blind. Orphan detection now reads NT's own position truth and queues the crew.
- **2026-07-18** — 444 stale working orders made NT reject *everything* (worst-case
  exposure vs MaxPositionSize) — `working_orders()` + stale-cancel tooling exist for this.
- **2026-07-23** — `flatten_position` doubled an orphan by triggering its stale entry
  stops → use `close_nt_position` for unprotected orphans.
- **2026-07-29/30** — Lucid's **unannounced ~$1,200 eval daily loss limit** killed LFE..89.
  Verify a firm's rules on every purchase; the daily-loss guards are sized under it.
- **2026-08-12** — a dead account looks like `OIF[NOFILE]` + `account does not exist` in
  the NT trace, and reads as equity $0 (⇒ bogus MLL breach). Stale-read detection skips
  guards rather than acting on $0.
- **2026-08-13** — NT sqlite doesn't hold live Lucid positions; position files do. Never
  default an account name in code; resolve from config. Cancel entries that time out.

---

## Sister Repo

`~/judas-futures-workshop` — pure-Python rule-based lab (separate DB, separate systemd
prefix `judasfutures-*`). Read-only baseline for this repo; no cross-imports, copy logic
instead. Its buffet scanner has been halted by its own sim MLL flag since 2026-07-24;
`judasfutures-buffet-lfe76` was disabled 2026-08-12 (LFE..76 blew 2026-07-17).
