# Judas Agentic Crew — Architecture Plan V2
## "Use It Productively" — Maximizing MiniMax Quota on Research, Not Polling

**Status:** Approved 2026-05-14  
**Replaces:** AGENTIC_OPERATOR_PLAN.md (Phase 1-10 multi-agent polling design)

---

## The Problem We're Solving

The original multi-agent design had Trader + Registrar running **every 5 minutes** and Operator every **30 minutes** regardless of whether there was anything to do. On a **request-limited** plan (4,500 requests / 5 hours, 45,000/week), this burned the entire budget on empty polling cycles before any real research happened.

At ~5,800 requests/day we were hitting 65%+ of the weekly quota from infrastructure overhead alone — leaving almost nothing for the YouTube ingestion, custom strategy generation, and exploratory research the system was actually built to do.

**The fix:** Make the core trading path 100% deterministic (zero LLM), make all other agents event-driven or deliberately scheduled, and direct the saved budget into aggressive research.

---

## The Non-Negotiable Core: Hourly Scanner is Pure Python Forever

```
judas-crew.service (every hour during Globex sessions)
  → bar_cache.refresh_cache()        — fetch from IBKR, write parquet + last_bar_closes.json
  → _reconcile_open_trades()         — close any stops/targets hit since last bar
  → run_portfolio_scan()             — evaluate all active_strategies deterministically
      ├── _evaluate_rsi()            — pure Python
      ├── _evaluate_ma_cross()       — pure Python
      ├── _evaluate_bollinger()      — pure Python
      ├── run_judas_detection_rich() — pure Python
      └── place_bracket()            — deterministic broker call
```

**Zero LLM calls. Zero MiniMax requests. This never changes.**

The portfolio scanner is the money-making engine. It must be fast, deterministic, and immune to LLM outages or quota exhaustion. Strategies are evaluated by code, not conversation.

---

## The Usage Architecture: 25,000–35,000 Requests/Week, Used Productively

| Tier | Agent | Frequency | Requests/Week | Purpose |
|------|-------|-----------|---------------|---------|
| **0 — Trading Core** | Portfolio scanner | Hourly, always | **0** | Deterministic execution |
| **1 — Researcher Blitz** | Researcher | Every 90 min during market hours + 2× nightly | **18,000–22,000** | Main usage driver: YouTube + web → backtest → propose |
| **2 — Operator Review** | Operator | 2×/day (06:00 UTC + 21:00 UTC) | **3,000–4,000** | Portfolio health, delegations, regime decisions |
| **3 — Weekly Overhaul** | Operator + Researcher | Every Sunday | **2,000–3,000** | Deep re-ranking, cross-symbol sweeps, big ideas |
| **4 — On-Demand Bursts** | Any | User or agent triggered | **2,000–5,000** | Manual deep dives, chart analysis, custom strategy invention |
| **Total** | | | **25k–35k/week** | ~55–75% of quota — safe headroom |

---

## Tier 1: The Researcher Blitz (The Usage Engine)

The Researcher is the heart of this system. Every 90-minute session during market hours, every nightly run, it does the same aggressive loop:

### Researcher Session Loop (every run)

```
1. INGEST
   search_youtube_trading_videos("ICT liquidity sweep 2026")
   search_youtube_trading_videos("SMC order block judas swing")
   search_youtube_trading_videos("inner circle trader [current month]")
   → Pull top 4–8 video URLs from last 24–48h
   → Check findings table: skip any video_id already processed
   → fetch_youtube_transcript() on unprocessed videos (language fallback: manual EN → generated EN → any)
   → Extract concrete rules: session windows, sweep criteria, displacement thresholds, FVG requirements

2. WEB CONTEXT
   web_search("gold futures ICT setup today")
   web_search("dollar index liquidity levels [date]")
   → Grab macro context: key levels, session biases, news events
   → Feed into regime tag for the day

3. BACKTEST
   → For each extracted concept, formulate parameters
   → run_judas_threshold_sweep() or run_walk_forward() or run_custom_backtest()
   → Test extracted ideas against cached 1H bars for all relevant symbols
   → Target: 2–5 backtest runs per session

4. PROPOSE OR DISCARD
   → If concept backtests with PF > 1.5 and ≥ 20 trades: propose_candidate()
   → If concept fails: record_finding() with "REJECTED: [reason]" so it's never re-tested
   → record_finding() with video_id as dedup key regardless of outcome

5. MULTI-SYMBOL SWEEP
   → When a promising parameter set emerges, sweep it across ALL 8 symbols in the same session
   → Symbols: MGC, MNQ, MCL, MBT, MET, DX, ZF, 6J
   → One backtest run per symbol (no extra LLM calls — pure Python loops inside the tool)
```

### Researcher Schedule

| Time (UTC) | Session | Focus |
|---|---|---|
| Every 90 min, 13:30–21:00 UTC | Market hours blitz | Fresh YouTube + live bar context |
| 22:00 UTC | Post-NY deep session | Longer transcript analysis, custom strategy variants |
| 04:00 UTC | Pre-London session | Overnight news sweep, Asian session concepts |

---

## Tier 2: Operator (2×/Day, High Signal)

The Operator's job is **read everything, decide, delegate — not micromanage**.

### 06:00 UTC Run (Pre-London)
```
1. Read daily brief from yesterday
2. Check: any new candidates from overnight Researcher?
3. Check: any strategies with 0 fires in 14+ days?
4. Check: macro news (web_search for key economic releases today)
5. Decisions:
   → Delegate retire/promote to Registrar if evidence is there
   → Queue new research tasks for Researcher if gaps exist
   → Update regime tag based on macro context
6. Write pre-session brief note
```

### 21:00 UTC Run (Post-NY Close)
```
1. Review today's scanner fires (from judas_crew.db)
2. Review today's Researcher output (new findings, candidates)
3. Review any open agent_tasks from team
4. Write daily brief (single LLM call covering: trades, regime, research, tomorrow's watch)
5. Delegate next day's priorities to Researcher task queue
```

The daily brief is generated inside this 21:00 run — **one call covers Operator review + brief generation**. No separate brief process.

---

## Tier 3: Weekly Sunday Overhaul

Every Sunday at 14:00 UTC, a dedicated deep-research session:

```
1. Pull 30-day performance for all active_strategies
2. Researcher does broad YouTube sweep: "what's working in current market regime"
3. Cross-symbol strategy sweep: take top 3 concepts from the week and test all 8 symbols
4. Operator reviews full roster: retire clear dead weight, promote top candidates
5. Generate weekly summary report (single LLM call)
6. Queue next week's research priorities
```

---

## Tier 4: On-Demand Dashboard Bursts

Three dashboard buttons feed the on-demand budget:

**"Run Researcher Now"** — Trigger a full Researcher session immediately. Useful when you've seen something on a chart or YouTube you want investigated.

**"YouTube: [type concept]"** — Direct the Researcher at a specific ICT concept or keyword. Researcher searches, transcribes the top 3 results, extracts setups, backtests, reports to dashboard chat within minutes.

**"Sweep All Symbols"** — Input any strategy params and automatically run walk-forward validation across all 8 symbols. Results appear in dashboard.

---

## Event-Driven Agent Timers (What Changes from Current)

### Current (Wasteful)
```
judas-trader.timer:     Every 5 min  → 288 runs/day, mostly empty
judas-registrar.timer:  Every 5 min  → 288 runs/day, mostly empty
judas-operator.timer:   Every 30 min → 48 runs/day, mostly empty
judas-researcher.timer: Weekend hourly + nightly weekdays
```

### New (Productive)
```
judas-trader.service:     Runs after judas-crew.service IF agent_tasks has pending 'trader' items
                          (systemd: judas-crew.service → trigger trader only when tasks exist)

judas-registrar.timer:    3×/day: 06:30 UTC, 13:30 UTC, 21:30 UTC (after Operator runs)
                          Short session: check candidates queue, execute any approved promote/retire

judas-operator.timer:     2×/day: 06:00 UTC, 21:00 UTC

judas-researcher.timer:   Every 90 min 13:30–21:00 UTC (market hours)
                          + 22:00 UTC (post-NY deep session)
                          + 04:00 UTC (pre-London)
                          = ~8 sessions/day during weekdays
                          Weekend: same + bonus Sunday overhaul at 14:00 UTC
```

---

## What the Researcher Knows (Context Always Loaded)

Every Researcher session pre-loads from Python before the LLM call:
- All active_strategies with last 30-day performance stats
- Last 5 findings from findings table
- Open agent_tasks assigned to researcher team
- Current regime tag
- Last daily brief summary
- Workshop leaderboard (top 10 strategies by PF)
- Symbols with zero coverage (no active strategy currently)

This means the Researcher hits the ground running on the first LLM call, not after 3–4 tool discovery cycles.

---

## YouTube Deduplication (Already Partially Built)

The `findings` table tracks processed content. Convention:

```python
# When a video is processed:
record_finding(
    title=f"YT:{video_id}:{video_title}",
    body="EXTRACTED: [concepts] / BACKTEST: [result] / VERDICT: [accepted|rejected]",
    refs={"video_id": video_id, "url": url, "channel": channel}
)

# Before fetching a transcript:
# Check: SELECT COUNT(*) FROM findings WHERE title LIKE 'YT:{video_id}:%'
# If exists → skip, already processed
```

Over time the Researcher builds a complete map of processed ICT content — no redundant re-analysis.

---

## Custom Strategy Evolution (Phase Next)

Once the Researcher has a library of backtested custom strategies:

1. Take top 5 custom strategies from `custom_strategies` table (by backtest PF)
2. Have Coder generate 3–5 parameter variants of each
3. Backtest all variants
4. Promote best variants to active_strategies
5. Retire underperformers

This creates a continuous improvement loop without any human input after initial seeding.

---

## What Doesn't Change

- **Paper-only hard lock**: `ibkr_executor.py` still raises ValueError if mode ≠ paper
- **Operator never retires without evidence**: 20+ trades of negative P&L, or 14+ days no fires
- **Operator never retires directly**: All retirements go through Registrar with audit trail
- **Max open positions**: Enforced deterministically in `_gate_fire()`, not by LLM
- **Stop/target reconciliation**: Pure Python in `_reconcile_open_trades()` each hourly scan

---

## Implementation Priority Order

1. **Fix Trader timer** — event-driven, only fires when agent_tasks has pending items
2. **Rebuild Operator schedule** — 2×/day with rich pre-loaded context, brief generation inside 21:00 run
3. **Rebuild Researcher schedule** — 90-min market-hours blitz + 2 nightly sessions
4. **Fix Registrar** — 3×/day, short queue-flush sessions only
5. **Dashboard buttons** — Run Researcher Now, YouTube search, Sweep All Symbols
6. **YouTube dedup convention** — enforce `YT:{video_id}:` prefix in findings across all Researcher prompts
7. **Multi-symbol sweep** — widen backtest loops to cover all 8 symbols per concept
8. **Custom strategy evolution** — Coder mutation loop on top performers

---

## Success Metrics

| Metric | Target |
|---|---|
| Weekly MiniMax requests | 25,000–35,000 (55–75% of quota) |
| Requests from infrastructure polling | < 500/week |
| Researcher sessions/week | 50–60 |
| YouTube transcripts processed/week | 30–50 |
| New candidates proposed/week | 5–15 |
| Strategy coverage (symbols with ≥1 active strategy) | All 8 symbols |
| Scanner zero-LLM runs | 100% |
