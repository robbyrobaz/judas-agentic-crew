# judas-agentic-crew

`judas-agentic-crew` is now a paper-only, agentic futures lab on IBKR that does two separate jobs:

1. `Trading runtime`: scans the currently active paper strategy portfolio and places paper bracket orders.
2. `Research runtime`: keeps exploring, backtesting, walk-forward testing, and proposing or promoting stronger paper strategies.

The workshop repo `../judas-futures-workshop` is treated as the seeded incumbent baseline, not just background reading. This repo imports its buffet winners, backtest artifacts, and leaderboard data, then tries to beat that baseline over time.

## Current Architecture

- `main.py`
  - entrypoint for doctor mode, seeded imports, and trading runtime selection
  - `--runtime auto` chooses:
    - `portfolio` if active strategies exist
    - `crew` otherwise
- `src/portfolio_runtime.py`
  - multi-strategy paper portfolio engine
  - supports:
    - `judas_native`
    - `buffet_zoo`
    - `buffet_pair`
- `src/strategy_registry.py`
  - active paper strategy registry
  - candidate creation and promotion helpers
- `src/tools/research_tools.py`
  - deterministic research tools
  - workshop leaderboard ingestion
  - Judas sweeps
  - walk-forward validation
  - candidate creation / auto-promotion for validated Judas variants
- `src/crews/research_crew.py`
  - research crew for exploration and reporting
- `src/dashboard/app.py`
  - dashboard backend and Operator Manager chat
- `dashboard/`
  - React + TypeScript + Tailwind frontend

## What It Trades

The active paper portfolio is seeded from the workshop buffet and currently includes:

- `judas_native` on `MGC`
- `buffet_zoo` strategies such as:
  - RSI mean reversion
  - EMA cross trend-follow
  - Bollinger mean reversion
- `buffet_pair` strategies such as:
  - `MGC/MNQ`
  - `MCL/MGC`
  - `MBT/MCL`

Everything is still `IBKR paper only`.

## Safety Model

- hard `paper` mode only
- backend timestamps/logs in UTC
- operator-facing responses in `America/Phoenix`
- session gate blocks weekend/off-hours entry
- hard flat deadline logic remains in the session/risk path
- active strategy registry controls what can trade
- research may promote to paper, but this repo does not support live trading

## Seeded Baseline

This repo imports the workshop baseline from `../judas-futures-workshop`:

- `buffet.yaml`
- `buffet_top.csv`
- `buffet_pf_ranked.csv`
- `buffet_results.csv`
- `fast_battery_MGC_1h.csv`
- `fast_battery_NQ_1h.csv`
- `sweep_all_results.csv`
- `pairs_results.csv`
- `RESEARCH_FINDINGS.md`
- `STATUS.md`

Imported copies are stored in:

- `outputs/research/workshop_seed/`
- `knowledge_base/buffet.yaml`

## Setup

Create the venv and install Python deps:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` and set at minimum:

```bash
MINIMAX_API_KEY=...
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_DATA_CLIENT_ID=150
IBKR_EXEC_CLIENT_ID=151
```

For the dashboard frontend:

```bash
cd dashboard
npm ci
npm run build
cd ..
```

## Common Commands

Health check:

```bash
.venv/bin/python main.py --doctor --symbol MGC
```

Import the workshop baseline and activate seeded paper strategies:

```bash
.venv/bin/python scripts/import_workshop_seed.py
```

Evaluate the active portfolio without placing orders:

```bash
.venv/bin/python main.py --runtime portfolio --eval-only --symbol MGC
```

Normal runtime:

```bash
.venv/bin/python main.py --runtime auto --symbol MGC
```

Run research manually:

```bash
.venv/bin/python scripts/run_research.py --symbol MGC --log-level INFO
```

## Systemd

Install services and timers:

```bash
bash systemd/install.sh
```

That install script now also:

- imports the workshop seed portfolio
- runs `npm ci`
- builds the dashboard frontend

Primary units:

- `judas-crew.timer`
  - hourly trading cadence
- `judas-research.timer`
  - hourly on weekends
  - nightly on weekdays
- `judas-dashboard.service`
  - dashboard backend

## Dashboard

Tailscale endpoints:

- `http://omen-claw.tail76e7df.ts.net:8080/`
- `https://omen-claw.tail76e7df.ts.net:8443/`

The dashboard gives you:

- recent signals
- recent trades
- research experiments
- runtime/service status
- Operator Manager chat

The Operator Manager is an operator interface, not unrestricted god-mode execution.

## How Promotion Works Now

For Judas variants:

1. research runs sweeps
2. research runs walk-forward
3. deterministic thresholds decide:
   - reject
   - candidate only
   - promote to paper
4. promoted variants are written into `active_strategies`
5. portfolio runtime reads active strategy params on the next scan

For workshop buffet strategies:

- the imported top buffet set is the incumbent seeded baseline
- the portfolio engine can paper-trade supported seeded strategies now
- research should compare new candidates against that incumbent set

## Important Limitations

- full live-market-hours validation of the new portfolio paper order path still requires an open futures session
- pair and buffet execution is now wired into this repo, but regime review / demotion logic is still earlier-stage than the runtime itself
- the Operator Manager chat is useful, but the real intelligence is still in deterministic research, promotion, and execution code

## Goal

The goal is not to merely copy the workshop bot.

The goal is:

- use the workshop as the seeded incumbent
- let research continuously challenge it
- promote stronger paper strategies
- retire weaker ones
- make the agentic version outperform the static workshop baseline over time
