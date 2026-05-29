# Lab Goal

Double the $5,000 IBKR paper sleeve every 30 days, compounded.
$5K → $10K → $20K → $40K → $80K. Continuously. No human intervention.

## The numbers

- ~100% per month, ~25% per week, ~3.6% per trading day
- The non-agentic workshop hit $1,700 in one week on the same sleeve,
  so the edge exists. The agentic system's job is to find that edge,
  spread it across all 8 symbols and all session windows, and run it
  24/7 without supervision.

## What the math demands

To double in 30 days on a $5,000 sleeve at ~1% per-trade risk:
- ~100 winning trades net of costs and losses over the month
- Requires high-frequency edge, high-expectancy edge, or both
- One -$2,000 day eats half the month. Drawdown control matters
  more than chasing the next backtest peak.
- Costs are real. Cost model v1 (`src/tools/cost_model.py`) stamps
  backtests after 2026-05-25. Trust net PF over gross.
- PF is a quality gate; **dollar throughput is the optimization
  target** (P&L per fire × fires per period).

## Resources

- **45,000 MiniMax requests/week.** Spend them. Idle research = lost ground.
- **8 symbols available:** MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J
- **24h Globex coverage** (Asia / London / NY sessions)
- **IBKR DUH860616 paper account.** Bracket orders via the scanner
  (zero LLM in the trading hot path)
- **Cost model v1** stamped on backtests after 2026-05-25
- **Real IBKR fills** landed 2026-05-25 (filter
  `exit_reason LIKE '%_real'` on trades)

## What "working" looks like

- Compounding net P&L curve, week over week
- Coverage across all 8 symbols
- Active strategies firing trades regularly (not sitting idle)
- Backtest PFs that actually translate to live PFs
  (`gap_flag = False` on most rows)

## Agent permissions (max autonomy)

Every specialist has:
- Full filesystem read + write + edit (anywhere in the repo)
- Shell command execution via `run_shell`
- Direct SQLite via `query_db`
- The own-system leaderboard via `get_leaderboard`
- Findings memory via `record_finding` / `read_findings`

Git is the safety net. Move fast.

## Scoreboard

The dashboard's leaderboard view and the operator's
`LEADERBOARD + LIVE GAP` kickoff block are both sourced from
`get_leaderboard()` — human and agents look at literally the
same numbers.

Each leaderboard row has two flags:
- `unproven_live`: backtest exists, live_n < 3 → **test it, gather data**
- `gap_flag`: backtest exists, live_n ≥ 3, live_pf ≤ 0 →
  **backtest lied, investigate**
