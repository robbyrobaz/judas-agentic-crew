# Dashboard — Phase 7 (UI Surface for Phases 2/3/4/5)

**Status:** proposal, 2026-05-10. Not yet built. Wait for Phase 3 to land before spawning workers on this — the new panels surface Phase 3 data.

## Context

Today's dashboard (`dashboard/src/App.tsx`, 798 lines) has:
- Header strip with 4 metric cards (PnL / fires / fills / state)
- BriefPanel (Phase 4)
- Two-column panel grid: signals, trades, research experiments, runtime status
- Right column: Operator Manager chat

What's missing — backend data exists, no UI:
- `/api/demotions` (Phase 2) — auto-retired strategies + reactivate button
- `/api/autofixes` (Phase 3) — symptom queue + diff viewer + merge/reject buttons
- Per-strategy live metrics (Phase 2 `compute_live_metrics`) — PF/expectancy/days-since-fire
- Regime tag (Phase 4 `tag_regime`) — vol regime + trend pill, currently only inside the brief
- System health: timer next-firings, blitz status, kill flags, research lock
- Active strategy roster with version + activated_at

## Proposed Phase 7 deliverables

Eight panels, in priority order. Each is a single React component + a backend endpoint if not already present.

### 7a — System Health bar (top header)

A thin strip below the title showing:
- `judas-operator` next firing (countdown)
- Blitz state (running / idle / how many cycles done)
- Research lock (free / held by which symbol)
- `kill.flag` and `autofix.disable` presence (red badges if present)
- Live IBKR connection status (poll `/api/health` every 30s)

**Why first:** at-a-glance trust. If something's wrong, you see it before reading any panel.

### 7b — Auto-fixes Queue panel

Lists `/api/autofixes?status=open` rows. Each row shows:
- Symptom category + summary
- Branch name + GitHub link
- Files changed count + test result badge
- **Merge** / **Reject** buttons (call Phase 3 endpoints)
- Expandable diff viewer (calls `/api/autofixes/<id>` for full detail)

**Why second:** without this UI, auto-fixes silently pile up on `origin/autofix/*` and you only see them on GitHub.

### 7c — Auto-demotions ledger panel

Lists `/api/demotions` (last 30 days, both reactivated and not). Each row:
- Symbol + strategy_family + version
- Date + reason
- Metrics snapshot (collapsed by default)
- **Reactivate** button if `reactivated_at_utc IS NULL`

**Why third:** if the daily brief auto-retires a strategy you disagree with, you need a one-click rollback path.

### 7d — Live Performance grid

A small table per active strategy showing rolling 20-trade metrics:
- Symbol, strategy_name, version
- PF (color-coded green/yellow/red against 0.9/1.1/1.5)
- Expectancy (R)
- Last fire (relative time)
- Days active

Backend: new endpoint `/api/active-strategies-with-metrics` that combines `list_active_strategies` + `compute_live_metrics` per row. Cache for 5 min — avoid hammering on poll.

### 7e — Regime ribbon

A horizontal pill row near the top:
- Vol regime (high / mid / low) with color
- Trend (trending / ranging / mixed)
- Top-3 leaders (symbol pills, click to scroll to that strategy in the grid)

Backend: read from latest `daily_briefs.summary_json.regime`. If no brief today yet, compute on-the-fly via `tag_regime`.

### 7f — Research Experiments stream

A vertical timeline of the last 30 `research_experiments` rows:
- Type, name, status, duration
- Click for the full report

Currently exists in some form but expand to show structured details.

### 7g — Symptoms panel

Lists detected symptoms (Phase 3a) that haven't yet been turned into auto-fix attempts. Shows what the system thinks is broken even before it tries to fix.

### 7h — Sparklines

A bottom strip of 3 small charts:
- 7-day cumulative PnL line
- Daily fires bar (last 30 days)
- Active-strategies count over time

## Layout sketch

```
┌─ Header strip ────────────────────────────────────────────┐
│  [PnL] [Fires] [Fills] [State]                            │
│                                                            │
│  ── 7a System Health bar ──────────────────────────       │ ← new
│  ── 7e Regime ribbon ──────────────────────────────       │ ← new
├─ Today's Brief ──────────────────────────────────────────┤
│  (existing BriefPanel — Phase 4)                           │
├─ Panel grid (2-col) ─────────────────────────┬─ Chat ─────┤
│  • Signals                  • 7b Auto-fixes  │            │
│  • Trades                   • 7c Demotions   │  Operator  │
│  • Research experiments     • 7d Live Perf   │  Manager   │
│  • Runtime status           • 7g Symptoms    │   (chat)   │
│                                              │            │
├─ 7h Sparklines (full width) ──────────────────────────────┤
└────────────────────────────────────────────────────────────┘
```

That fills the empty space without crowding.

## Backend additions needed

Most endpoints exist. Add:
- `/api/health` — combines connection state, kill flags, lock state, timer next firings into one JSON. Polled every 30s by 7a.
- `/api/active-strategies-with-metrics` — joined view for 7d.
- `/api/sparkline-data?series=pnl_cum|fires|active_count&days=30` — for 7h.

## Sequencing

| Build | What | Worker model |
|---|---|---|
| 7a + 7e | System Health + Regime ribbon | one agent (header surface) |
| 7b | Auto-fixes panel | one agent (depends on Phase 3 merged) |
| 7c | Demotions panel | one agent (Phase 2 backend ready) |
| 7d + 7h | Live Performance + sparklines | one agent (charts) |
| 7f + 7g | Experiments stream + Symptoms | one agent (read-only views) |

5 agents in parallel, worktree-isolated. Estimate: 30–45 min wall clock for the whole thing. Each is small, additive, no shared file conflicts if header/panel additions are kept in separate JSX blocks.

## What I'm NOT proposing

- A redesign. Existing layout is good; we're filling.
- New heavy dependencies. Use what's already in `package.json` (lucide-react, react-markdown). Charts: simple SVG sparklines, no chart lib.
- Mobile responsiveness pass. Keep desktop-first.
- Real-time WebSocket. 30–60s polling is fine for ops.

## Decision points for operator

1. Do you want the 5 agents launched after Phase 3 lands, or after a manual review of Phase 3 output first?
2. Sparklines — keep simple SVG (proposal) or import a chart lib (recharts, victory)?
3. Should 7c (demotions) include a 7-day quarantine column to mark "do not auto-promote this family for N days" — or save that for Phase 6 HITL tuning?
