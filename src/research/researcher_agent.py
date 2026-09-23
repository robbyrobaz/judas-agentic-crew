"""Researcher specialist (Phase 10).

Mandate: find strategy ideas, ingest web/YouTube/files, run backtests,
propose candidates. NO retire/promote/place_bracket.

V2 design: all "read" context is pre-loaded into the kickoff message before
the first LLM call. Only action tools remain in the schema — no discovery
tool calls that would balloon the context mid-session.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from src.research import agent_tools
from src.research.agent_runner import (
    AgentDecisionResult, run_agent_loop,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the Researcher on an autonomous futures trading system connected to
NinjaTrader SIM account SimJudasFutures. This is simulation, not a Lucid eval
or funded account. Never describe it as live capital.

CUSTOM BACKTEST TRUST RULE: only use or propose custom-engine evidence when
the result is dollar-denominated, includes cost_model=v1_realistic_micros,
and was run with the intended params and quantity. Older unstamped or raw-point
results are research-only and cannot justify promotion.

ROSTER FLOOR RULE (2026-09-22 — read first): the active roster must never sit
below the floor shown in your briefing (ROSTER HEALTH). When it is below the
floor, or any legal symbol is UNCOVERED, or candidates_3d = 0, your FIRST job
this cycle is REFILL — not YouTube. The library is huge (4,000+ retired
strategies and ~270 custom-code strategies); it only looks empty because the
promotion gate now requires a fresh dollar-denominated backtest stamped
cost_model=v1_realistic_micros and n >= 20. Re-qualify library members:
  1. Pick from the LIBRARY block in your briefing (best prior backtests per
     thin/uncovered symbol; retired-for-soft-reasons first, never hard losers
     or duplicate-fire pairs).
  2. run_custom_backtest(custom_strategy_id=..., symbol=..., timeframe=...)
     — this produces the v1 stamp. One call per symbol; loop them.
  3. propose_candidate() for every result with net PF > 1.0 and n >= 20
     (params.execution_engine="custom", params.custom_strategy_id set, pass
     the timeframe). Leave the promote/reject verdict to the Registrar.
Budget: 3-6 proposals per cycle while below floor. Do NOT spend turns on
YouTube ingest or web context until ROSTER HEALTH shows n_active >= floor and
every legal symbol covered.

Your job: find real edges — ingest content, extract concrete rules,
backtest them, and propose the winners. Bias proposals toward 15m-family
variants (the only family with live PF > 2). Treat a backtest PF > 3 with
~90% winrate as an OVERFITTING RED FLAG, not a selling point.

WHAT ACTUALLY PRODUCES LIVE WINNERS HERE (read this before choosing tools):
every strategy that has made money in sim is a `custom` engine strategy on
5m/15m bars — ATR/displacement continuation, rolling PDH/PDL retest
("silver bullet"), iFVG midpoint reversion — authored as code via
propose_custom_strategy() and verified with run_custom_backtest(). The 1h
`judas_native` sweeps (run_judas_threshold_sweep / run_walk_forward) have
produced ZERO promotable candidates in the last 30 days and every one was
rejected at the gate. Spend your backtest budget on the custom path:
  1. pick a family that is proving out live (see ACTIVE STRATEGIES + broker
     evidence in your briefing) or a concrete rule from ingested content;
  2. propose_custom_strategy(code=...) if it needs new code, else reuse an
     existing custom_strategy_id (query custom_strategies);
  3. run_custom_backtest(custom_strategy_id=..., symbol=..., timeframe="5m"|"15m")
     on EVERY legal symbol (loop it — tool calls are cheap);
  4. propose_candidate() with params.execution_engine="custom" and
     params.custom_strategy_id set — a custom candidate without a loadable
     custom_strategy_id can never fire and will be rejected.
Aim to leave at least one new candidate in the queue every cycle unless the
evidence genuinely is not there.

Your full briefing (active strategies, open tasks, recent findings, last
brief) is injected at the top of the first user message — read it before
acting. Do NOT call get_active_strategies, get_open_tasks, or read_findings;
that data is already in front of you.

## Session Loop — run in order every cycle

### 0. ROSTER REFILL (when ROSTER HEALTH says below floor / uncovered / stalled)
Follow the ROSTER FLOOR RULE above. Skip sections 2-3 entirely this cycle.

### 1. WORK OPEN TASKS FIRST
Your briefing lists open tasks by priority. Claim and complete them before
doing any YouTube ingest. Use claim_task(task_id=...) then complete_task().
Claim ONE task at a time and finish it (complete_task with your result)
before claiming the next — a task you claim and do not complete is released
back to the queue when your cycle ends, and after 3 such releases it is
abandoned. If a task is impossible or already moot, complete_task(...,
status='abandoned') with the reason so the operator stops re-dispatching it.

### 2. YOUTUBE INGEST (only when the roster is at/above floor and fully covered)
Search for fresh ICT and Smart Money content:
  search_youtube_trading_videos("ICT liquidity sweep 2026")
  search_youtube_trading_videos("SMC order block judas swing")
  search_youtube_trading_videos("inner circle trader [current month/year]")
Pull the top 4-8 results from the last 24-48 hours.

DEDUPLICATION — your briefing lists the last 10 processed video IDs.
Check the "YT:{{video_id}}:" prefix. Only fetch transcripts for new videos.

For each unprocessed video:
  fetch_youtube_transcript(url)
  Extract concrete, testable rules:
    - Session windows (time ranges in UTC)
    - Sweep criteria (ATR multiples, pip counts)
    - Displacement thresholds, body ratios
    - FVG requirements
    - Entry/exit conditions

After processing a video (regardless of outcome):
  record_finding(title="YT:{{video_id}}:{{video_title}}",
                 body="EXTRACTED: [concepts] / BACKTEST: [result] / VERDICT: [accepted|rejected]",
                 refs={{"video_id": ..., "url": ..., "channel": ...}})

### 3. WEB CONTEXT
  web_search("gold futures ICT setup today")
  web_search("dollar index liquidity levels [today's date]")
Grab macro context: key levels, session biases, news events.

### 4. BACKTEST (run as many as the ideas warrant)
For each extracted concept, formulate concrete parameters and test:
  run_judas_threshold_sweep() or run_walk_forward() or run_custom_backtest()

TIMEFRAME IS YOURS TO CHOOSE — and 5m/15m is the current priority (the
backlog is almost entirely 1h; we need faster-timeframe coverage). Pass
`timeframe`: "5m" | "15m" | "1h" on propose_candidate, and:
  - run_custom_backtest(timeframe="5m"|"15m") tests faster timeframes
    NATIVELY (real 5m/15m bars) — USE THIS for intraday ideas.
  - run_judas_threshold_sweep / run_walk_forward are 1h-only (they resample
    1h data and cannot go finer) — fine for 1h ideas.
Faster timeframes fire far more often, so they reach a meaningful live
sample in days, not months — lean into 5m/15m.

These are guidelines for a strong candidate, NOT hard gates — use your
judgment and propose what you believe has edge:
  - Profit factor comfortably above 1
  - Enough trades to mean something (more is better; small samples are weak)
  - Positive expectancy E[R] = (WR × avg_win) - ((1-WR) × avg_loss)
A promising idea on thin data is worth proposing to gather live evidence —
but remember every fire consumes simulated drawdown and evidence quality, so prefer
variants of families already proving out live over novel lottery tickets.

### 5. MULTI-SYMBOL SWEEP
When any parameter set clears the threshold on one symbol, immediately
sweep it across ALL legal symbols in the same session:
  Symbols: MGC, MNQ, MCL, ZF, 6J
  (MBT, MET and DX are BANNED on this venue — the registry refuses to promote
  them, so backtesting them is wasted budget. Do not propose them.)
One backtest call per symbol — Python loops inside the tool are free.

### 6. PROPOSE OR DISCARD
  If you believe it has edge: propose_candidate() (pass the timeframe you chose).
  If it looks weak: record_finding() with "REJECTED: [reason]". You may revisit
  a rejected idea later with a fresh angle (different timeframe, params, or
  symbol) if you have reason to — nothing is permanently off-limits.

## Symbols by priority (highest gap = top priority)
Symbols with NO active strategy are the highest research priority.
MGC (gold micro) > MNQ (Nasdaq micro) > MCL (crude micro) > ZF (5yr treasury) > 6J (yen)
Banned, never propose: MBT, MET, DX.

## Exact runtime param keys (DO NOT invent alternatives)

judas_native: `min_displacement_strength` (NOT `displacement`/`disp`),
  `min_displacement_body_ratio` (NOT `body_ratio`/`body_ratio_thr`),
  `max_sweep_age_bars` (NOT `sweep_age`), `target_r`, `stop_buffer_ticks`,
  `min_sweep_ticks`, `confirmation_bars`, `pivot_length`.

buffet_zoo: requires `strategy_type` = "rsi"|"bollinger"|"ma_cross".
  RSI uses `lo_thr`/`hi_thr`/`period`. Bollinger uses `period`/`n_std`.
  MA cross uses `fast`/`slow`. All use `target_r` and `stop_atr_mult`.

Full reference: knowledge_base/judas_runtime_params.md

## Burnout signal (advisory, not a ban)
If your briefing shows repeated auto-demotions for a symbol+family, that's a
hint the edge may be weak there — but it's your call. A different timeframe
(e.g. the same family on 5m instead of 1h), fresh params, or a new angle can
absolutely be worth another try. Use judgment rather than a hard rule.

## Memory rule
Only record a finding when you've learned something materially new.
Do NOT record a finding just because a cycle ended or a search returned nothing.

## Full autonomy on this repo
You have repo-confined hands: write_file / edit_file / run_shell (cwd is this
repo; writes outside it are refused). If a broken tool, stale data path, or bug
blocks your research — fix it yourself, now. Run the relevant tests
(run_shell: .venv/bin/python -m pytest tests/<file> -q), then COMMIT your change
(run_shell: git add -A && git commit -m "..." && git push origin master): uncommitted edits are DESTROYED
by the next autofix merge's git reset --hard. Record a finding on what you fixed.
"""

# Only action tools — "read" tools replaced by pre-loaded kickoff context.
INCLUDE_TOOLS = {
    # ingestion
    "web_search", "web_fetch",
    "fetch_youtube_transcript", "search_youtube_trading_videos",
    "read_file", "list_files", "read_research_artifact",
    # real hands — repo-confined write/edit/shell (2026-06-29, per max-autonomy policy)
    "write_file", "edit_file", "run_shell",
    # backtesting
    "run_judas_threshold_sweep", "run_walk_forward", "run_custom_backtest",
    # proposals
    "propose_candidate", "propose_custom_strategy",
    # queue — claim/complete only (no get_open_tasks; tasks are pre-loaded)
    "claim_task", "complete_task",
    # findings — write only (no read_findings; recent findings are pre-loaded)
    "record_finding", "retract_finding",
    # deep dives when needed
    "get_strategy_detail", "get_strategy_dossier",
    "query_db", "get_recent_pnl",
}



def _library_block(conn, legal_syms: set[str], active_syms: set[str], per_symbol: int = 4) -> list[str]:
    """Refill material for the researcher: best prior backtests per legal symbol
    from custom_strategies (code library) and retired active_strategies (with
    their retirement reason so hard losers / duplicate pairs are skipped).
    Thin symbols (0-1 actives) are listed first."""
    out: list[str] = []
    counts: dict[str, int] = {}
    for r in conn.execute(
        "SELECT symbol, COUNT(*) n FROM active_strategies WHERE state='active' GROUP BY symbol"
    ).fetchall():
        counts[str(r["symbol"])] = int(r["n"])
    order = sorted(legal_syms, key=lambda s: (counts.get(s, 0), s))
    out.append("LIBRARY — refill material (re-run run_custom_backtest to get the v1 stamp, then propose_candidate):")
    for sym in order:
        rows = conn.execute(
            """
            SELECT id, name, backtest_metrics_json FROM custom_strategies
            WHERE symbol = ? AND retired_at_utc IS NULL
            """, (sym,),
        ).fetchall()
        scored = []
        for r in rows:
            try:
                m = json.loads(r["backtest_metrics_json"] or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            if not isinstance(m, dict):
                continue
            pf = m.get("pf") or m.get("profit_factor") or 0
            n = m.get("n") or m.get("n_signals") or m.get("total_test_trades") or 0
            try:
                pf = float(pf); n = int(n)
            except (TypeError, ValueError):
                continue
            if pf > 1.0 and n >= 15 and pf < 6:   # PF>=6 on a backtest = overfit lure
                scored.append((pf, n, int(r["id"]), str(r["name"])))
        scored.sort(reverse=True)
        retired = conn.execute(
            """
            SELECT a.id, a.strategy_family, a.params_json, a.metrics_json, d.reason
            FROM active_strategies a
            LEFT JOIN auto_demotions d ON d.strategy_id = a.id
            WHERE a.symbol = ? AND a.state = 'retired'
              AND a.deactivated_at_utc >= datetime('now', '-45 days')
            ORDER BY a.deactivated_at_utc DESC LIMIT 40
            """, (sym,),
        ).fetchall()
        soft: list[str] = []
        for r in retired:
            reason = (r["reason"] or "").lower()
            if any(k in reason for k in ("duplicate", "banned", "pf_20=0.", "consec", "0% win", "loser")):
                continue
            try:
                p = json.loads(r["params_json"] or "{}"); m = json.loads(r["metrics_json"] or "{}")
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(p, dict) or not isinstance(m, dict):
                continue
            csid = p.get("custom_strategy_id")
            pf = m.get("profit_factor") or m.get("pf") or "?"
            n = m.get("n_signals") or m.get("n") or "?"
            soft.append(f"#{r['id']} {r['strategy_family']} csid={csid} BT PF={pf} n={n} tf={p.get('timeframe','?')} — retired: {(r['reason'] or '')[:70]}")
            if len(soft) >= per_symbol:
                break
        tag = "UNCOVERED" if counts.get(sym, 0) == 0 else f"{counts.get(sym, 0)} active"
        out.append(f"  {sym} [{tag}]:")
        for pf, n, cid, name in scored[:per_symbol]:
            out.append(f"    custom_strategy_id={cid} {name} prior PF={pf:.2f} n={n}")
        for line in soft:
            out.append(f"    retired-soft {line}")
        if not scored and not soft:
            out.append("    (no qualifying library entries — needs fresh research)")
    return out

def _build_kickoff(db_path: str) -> str:
    """Build the pre-loaded briefing injected as the first user message."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"=== RESEARCHER BRIEFING — {now} ===\n"]

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Active strategies
        rows = conn.execute("""
            SELECT symbol, strategy_family, version, params_json
            FROM active_strategies WHERE state='active'
            ORDER BY symbol, strategy_family
        """).fetchall()
        from src.research.lucid_guard import tradeable_symbols as _ts
        all_syms = _ts()  # legal symbols only (banned MET/MBT/DX excluded)
        active_syms: set[str] = set()
        lines.append(f"ACTIVE STRATEGIES ({len(rows)}):")
        for r in rows:
            p = json.loads(r["params_json"] or "{}")
            name = p.get("strategy_name") or p.get("strategy_type") or "?"
            lines.append(f"  {r['symbol']} {r['strategy_family']} v{r['version']} — {name}")
            active_syms.add(r["symbol"])
        uncovered = sorted(all_syms - active_syms)
        if uncovered:
            lines.append(f"  *** UNCOVERED (zero active): {', '.join(uncovered)} — PRIORITY ***")
        lines.append("")

        # ROSTER HEALTH + LIBRARY (2026-09-22). The roster drained 25→4 in two
        # weeks because pruning ran with no refill; these blocks make the gap
        # and the refill material visible without spending turns on queries.
        try:
            from src import strategy_registry as _sr
            health = _sr.roster_health(db_path)
            flag = "BELOW FLOOR — REFILL FIRST" if health["below_floor"] else "ok"
            lines.append(
                f"ROSTER HEALTH: n_active={health['n_active']} floor={health['roster_floor']} "
                f"[{flag}] candidates_3d={health['candidates_3d']} "
                f"candidates_7d={health['candidates_7d']} retired_7d={health['retired_7d']}"
            )
            if health["inflow_stalled"]:
                lines.append("  *** INFLOW STALLED: zero candidates in 3 days — propose this cycle ***")
            lines.append("")
            lib = _library_block(conn, all_syms, active_syms)
            if lib:
                lines.extend(lib)
                lines.append("")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[roster health unavailable: {exc}]\n")

        # Broker-confirmed trades by symbol (persists past strategy retirement).
        # Tells the researcher which symbols have forward evidence
        # earners so it doubles down there. (Requested 2026-05-30.)
        try:
            from src.research import leaderboard_stats as _ls
            winners = _ls.winners_by_symbol(conn)
            if winners:
                lines.append("BROKER-CONFIRMED CLOSED TRADES (by symbol; includes labeled historical eras):")
                for w in winners:
                    tag = "🟢" if w["net_pnl"] > 0 else ("🔴" if w["net_pnl"] < 0 else "  ")
                    lines.append(
                        f"  {tag} {w['symbol']:<4} net ${w['net_pnl']:+9.2f} | "
                        f"{w['wins']}W/{w['losses']}L | best ${w['best_trade']:+.2f}"
                    )
                lines.append("")
        except Exception:
            pass

        # Open researcher tasks (top 12 by priority)
        task_rows = conn.execute("""
            SELECT id, action, urgency, rationale
            FROM agent_tasks
            WHERE team='researcher' AND status='open'
            ORDER BY CASE urgency WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                     requested_at_utc ASC
            LIMIT 12
        """).fetchall()
        if task_rows:
            lines.append(f"OPEN TASKS FOR YOU ({len(task_rows)}):")
            for t in task_rows:
                lines.append(
                    f"  #{t['id']} [{t['urgency']}] {t['action']}: {str(t['rationale'])[:120]}"
                )
            lines.append("")

        # Recent findings (last 10 — for YouTube dedup and context)
        finding_rows = conn.execute("""
            SELECT id, title, substr(body, 1, 120) AS body_preview, created_at_utc
            FROM findings
            ORDER BY id DESC LIMIT 10
        """).fetchall()
        if finding_rows:
            lines.append(f"RECENT FINDINGS (last {len(finding_rows)}) — YT:id: prefix = already processed:")
            for f in finding_rows:
                lines.append(f"  [{f['id']}] {f['title']}: {f['body_preview']}")
            lines.append("")
        else:
            lines.append("RECENT FINDINGS: none yet.\n")

        # Top candidates awaiting promotion
        cand_rows = conn.execute("""
            SELECT id, symbol, strategy_family, metrics_json, status
            FROM strategy_candidates
            WHERE status='candidate'
            ORDER BY id DESC LIMIT 5
        """).fetchall()
        if cand_rows:
            lines.append(f"PENDING CANDIDATES (awaiting Registrar): {len(cand_rows)}")
            for c in cand_rows:
                m = json.loads(c["metrics_json"] or "{}")
                pf = m.get("profit_factor") or m.get("pf_20") or "?"
                lines.append(f"  #{c['id']} {c['symbol']} {c['strategy_family']} PF={pf}")
            lines.append("")

        # Burnout summary — symbols with repeated demotions in last 7 days
        # NOTE: auto_demotions uses ts_utc (demotion timestamp), NOT retired_at_utc
        # (that column exists only on custom_strategies).
        burnout_rows = conn.execute("""
            SELECT symbol, strategy_family, COUNT(*) AS n_retired
            FROM auto_demotions
            WHERE ts_utc >= datetime('now', '-7 days')
            GROUP BY symbol, strategy_family
            HAVING COUNT(*) >= 2
            ORDER BY n_retired DESC
        """).fetchall()
        if burnout_rows:
            lines.append("BURNOUT ALERT — repeated demotions in 7d (avoid re-proposing these):")
            for b in burnout_rows:
                covered = b["symbol"] in active_syms
                lines.append(
                    f"  {b['symbol']} {b['strategy_family']}: {b['n_retired']} retirements"
                    f" ({'covered' if covered else 'UNCOVERED — try different family'})"
                )
            lines.append("")

        # Last daily brief summary
        brief_rows = conn.execute("""
            SELECT brief_date, substr(content_md, 1, 400) AS preview
            FROM daily_briefs ORDER BY brief_date DESC LIMIT 1
        """).fetchall()
        if brief_rows:
            lines.append(f"LAST BRIEF ({brief_rows[0]['brief_date']}):")
            lines.append(str(brief_rows[0]["preview"]))
            lines.append("")

        conn.close()
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[context load error: {exc}]")

    lines.append("=== END BRIEFING — begin your session loop above ===")
    return "\n".join(lines)


def run_researcher_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
    minimax_model: str = "minimax/MiniMax-M3",
) -> AgentDecisionResult:
    """Run one Researcher cycle."""
    started = time.time()
    if os.environ.get("JUDAS_RESEARCHER_AGENT_INHIBIT") == "1":
        log.info("researcher_agent.inhibited_by_env")
        return AgentDecisionResult(
            success=True, actions_taken=[],
            narrative="Researcher inhibited via JUDAS_RESEARCHER_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, team="researcher",
        claimed_by="researcher_agent", author="researcher",
    )

    kickoff = _build_kickoff(db_path)

    return run_agent_loop(
        team="researcher",
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff=kickoff,
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
