"""Reviewer specialist (Phase 11).

Mandate: drain the candidate queue AND prune underperforming actives,
using cost-adjusted (net) metrics where available. NO ingestion, NO
trade placement, NO new strategy invention — promote/reject/retire/
retune only.

Pattern mirrors registrar_agent.py + operator_agent.py kickoff
construction. Heuristics live in the system prompt — the agent makes
the call, the code just supplies tools and visibility.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone

from src.research import agent_tools
from src.research.agent_runner import (
    AgentDecisionResult, run_agent_loop,
)

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are the Reviewer on a paper futures trading lab. Your job is to
keep the strategy registry honest by draining the candidate queue and
pruning underperforming actives, using cost-adjusted (net-of-cost)
metrics whenever they're available.

Tools you can use directly:
  - promote_candidate(id, notes) — move a candidate row into
    active_strategies (v+1). Multiple actives per (symbol, family)
    are allowed and encouraged if they cover different regimes /
    parameter regions.
  - reject_candidate(id, reason) — mark a candidate rejected with an
    audit trail. Use when it fails cost-adjusted thresholds,
    duplicates an existing active, or has obviously broken params.
  - modify_strategy_params(id, new_params) — atomic retire+promote of
    an active with new params. Use to retune rather than fully
    retire when the family is still working.
  - retire_strategy(id, reason) — retire an active. Real retire
    signals: cost-adjusted PF < ~0.9 on >= 20 trades, max consecutive
    losers >= 6, no fires in 14+ days, or an obvious duplicate of
    another live row.
  - reactivate_demoted(demotion_id) — bring back a previously
    retired row.

Read tools: get_active_strategies, get_strategy_detail,
get_strategy_dossier, get_candidates_queue, get_recent_pnl,
get_recent_trades, get_regime_tag.

Task queue: claim_task / complete_task / get_open_tasks — act on
claimed tasks but you are not limited to them. The candidate queue
and the active roster are both fair game on every cycle.

Findings memory: record_finding / read_findings / retract_finding.
Only record a finding when you've learned something materially new
the team will use on a later cycle — e.g. a single param tweak that
flipped a strategy from net-loser to net-winner. Do NOT write a
finding just because a cycle ended.

## HARD RETIREMENT GATE — check BEFORE retire_strategy()

A strategy is IMMUNE from retirement in these cases:
  1. n_closed_trades = 0 AND active < 7 days — give it time to collect data
     (exceptions: exact duplicate params, or broken execution_engine)
  2. total_realized_pnl > 0 AND n_closed_trades < 10 — a profitable live trade
     is stronger evidence than any historical walk-forward; do NOT retire based
     on WF data alone when real money confirmed the edge

Do NOT retire a brand-new strategy because:
  - its backtest came from a different experiment type (that's a researcher issue)
  - it hasn't fired yet (it just started — give it time)
  - you can't verify the BT source (verify via get_strategy_dossier, don't retire blindly)
  - it seems unproven (all new strategies are unproven — live trades are how we learn)

The correct action for a new strategy with questionable BT provenance: record_finding()
with the concern and move on. Let it run. Retire only if live metrics confirm the edge
is absent after real trades (n >= 10, pf_net < 0.8).

## REVIEWER HEURISTICS (for strategies with real live data)

These apply ONLY when n_closed_trades >= 10:
  - Cost-adjusted (*_net) metrics are the source of truth.
  - pf_20_net < 0.9 on >= 20 closed trades → retire candidate.
  - Stale: no fires in 14+ days, active > 14 days → retire candidate.

For candidate promotion, ALL THREE gates required:
  (1) walk-forward avg_test_profit_factor (net) >= 1.3
  (2) avg_test_E[R] > 0
  (3) total_test_trades >= 20
NO exception for uncovered symbols. Empty is safer than a net-loser.

Duplicate detection: same (symbol, family) with near-identical params → retire
all but highest PF. Check full params dict, not single param names.

Execution engine: 'judas_native' or 'buffet_zoo' only. 'custom' requires code review.
"""


INCLUDE_TOOLS = {
    # Read state
    "get_active_strategies", "get_strategy_detail", "get_strategy_dossier",
    "get_candidates_queue", "get_recent_pnl", "get_recent_trades",
    "get_regime_tag",
    # Mutate registry
    "retire_strategy", "promote_candidate", "modify_strategy_params",
    "reject_candidate",
    # Reactivate if needed
    "reactivate_demoted",
    # Tasks
    "claim_task", "complete_task", "get_open_tasks",
    # Findings — shared memory
    "record_finding", "read_findings", "retract_finding",
    # full filesystem + shell — max autonomy
    "read_file", "list_files", "read_research_artifact",
    "write_file", "edit_file", "run_shell",
    # own-system leaderboard + workshop cross-reference
    "get_leaderboard", "get_workshop_leaderboard", "query_workshop_db",
}


# ---------------------------------------------------------------------------
# Kickoff briefing
# ---------------------------------------------------------------------------


def _safe_get_live_metrics(db_path: str, sid: int) -> dict | None:
    """Best-effort call to compute_live_metrics. Tolerates older signatures
    that don't accept ``real_fills_only``."""
    try:
        from src.research.live_review import compute_live_metrics
    except Exception as exc:  # noqa: BLE001
        log.warning("reviewer.live_review.import_failed: %s", exc)
        return None
    # Prefer real_fills_only=True when supported, fall back if not.
    for kwargs in ({"real_fills_only": True}, {}):
        try:
            m = compute_live_metrics(db_path=db_path, strategy_id=sid, **kwargs)
            d = {}
            for attr in (
                "pf_20", "pf_20_net", "expectancy_20", "expectancy_20_net",
                "n_closed_trades", "max_consec_losers", "days_since_last_fire",
                "total_realized_pnl", "total_realized_pnl_net",
            ):
                if hasattr(m, attr):
                    val = getattr(m, attr)
                    try:
                        if val is None:
                            continue
                        d[attr] = float(val) if isinstance(val, (int, float)) else val
                    except Exception:  # noqa: BLE001
                        d[attr] = val
            return d
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("reviewer.live_metrics.failed sid=%s: %s", sid, exc)
            return None
    return None


def _param_fingerprint(params_json: str) -> tuple:
    """Fingerprint for duplicate detection: strategy_type + core entry params.
    Avoids hardcoded param names that change over time — uses execution_engine,
    strategy_type, and any numerics that define the entry logic."""
    try:
        p = json.loads(params_json or "{}")
    except Exception:  # noqa: BLE001
        return ()
    # Use engine + strategy_type + sorted numeric params as fingerprint
    engine = p.get("execution_engine", "")
    stype = p.get("strategy_type", "")
    numeric_params = tuple(sorted(
        (k, round(float(v), 4))
        for k, v in p.items()
        if isinstance(v, (int, float)) and k not in ("version", "quality_score_min")
    ))
    return (engine, stype) + numeric_params


def _build_reviewer_kickoff(db_path: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"=== REVIEWER BRIEFING — {now} ===\n"]

    # Self-audit block (same pattern as registrar/operator).
    try:
        from src.research.self_audit import build_leaderboard_block, build_self_audit
        lines.append(build_leaderboard_block(db_path))
        lines.append("")
        lines.append(build_self_audit(db_path))
        lines.append("")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[self_audit skipped: {exc}]\n")

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # --- Active strategies, ranked by net PF (lowest first) -----------
        active_rows = conn.execute(
            "SELECT id, symbol, strategy_family, version, params_json "
            "FROM active_strategies WHERE state='active' "
            "ORDER BY symbol, strategy_family, id"
        ).fetchall()

        actives_with_metrics: list[tuple] = []
        for r in active_rows:
            m = _safe_get_live_metrics(db_path, int(r["id"])) or {}
            pf_net = m.get("pf_20_net")
            pf_gross = m.get("pf_20")
            sort_pf = pf_net if pf_net is not None else (pf_gross if pf_gross is not None else 999.0)
            actives_with_metrics.append((sort_pf, r, m))

        actives_with_metrics.sort(key=lambda t: t[0])

        lines.append(f"ACTIVE STRATEGIES BY NET PF (lowest first) — {len(active_rows)} total:")
        for sort_pf, r, m in actives_with_metrics:
            pf_net = m.get("pf_20_net")
            pf_gross = m.get("pf_20")
            pf_str = (
                f"pf_net={pf_net:.2f}" if isinstance(pf_net, (int, float))
                else f"pf_gross={pf_gross:.2f}" if isinstance(pf_gross, (int, float))
                else "pf=?"
            )
            n_tr = m.get("n_closed_trades", "?")
            days = m.get("days_since_last_fire", "?")
            pnl_net = m.get("total_realized_pnl_net")
            pnl_gross = m.get("total_realized_pnl")
            pnl = pnl_net if pnl_net is not None else pnl_gross
            pnl_str = f"${pnl:+.2f}" if isinstance(pnl, (int, float)) else "$?"
            lines.append(
                f"  #{r['id']} {r['symbol']} {r['strategy_family']} v{r['version']} "
                f"— {pf_str} n={n_tr} stale={days}d pnl={pnl_str}"
            )
        lines.append("")

        # --- Top candidates by walk-forward PF (highest first) ------------
        cand_rows = conn.execute(
            "SELECT id, ts_utc, symbol, strategy_family, decision, "
            "       params_json, metrics_json, "
            "       substr(rationale, 1, 100) AS rationale_preview "
            "FROM strategy_candidates WHERE status='candidate'"
        ).fetchall()
        n_cands_total = len(cand_rows)

        def _cand_pf(c) -> float:
            try:
                m = json.loads(c["metrics_json"] or "{}")
            except Exception:  # noqa: BLE001
                return -999.0
            # Prefer net then walk-forward avg then pf_20 then pf.
            for k in (
                "avg_test_profit_factor_net",
                "pf_20_net",
                "avg_test_profit_factor",
                "pf_20",
                "profit_factor",
                "pf",
            ):
                v = m.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return -999.0

        cand_sorted = sorted(cand_rows, key=_cand_pf, reverse=True)
        top_cands = cand_sorted[:15]
        lines.append(
            f"TOP CANDIDATES BY WALK-FORWARD NET PF — {n_cands_total} total, top 15:"
        )
        for c in top_cands:
            try:
                m = json.loads(c["metrics_json"] or "{}")
            except Exception:  # noqa: BLE001
                m = {}
            try:
                p = json.loads(c["params_json"] or "{}")
            except Exception:  # noqa: BLE001
                p = {}
            pf = _cand_pf(c)
            n_tr = m.get("total_trades") or m.get("n_trades") or m.get("n_closed_trades") or "?"
            # Compact params summary
            psum_keys = ("strategy_type", "target_r", "fast", "slow", "min_sweep_ticks", "atr_period")
            psum = ",".join(
                f"{k}={p[k]}" for k in psum_keys if k in p
            ) or "—"
            lines.append(
                f"  #{c['id']} {c['symbol']} {c['strategy_family']} "
                f"pf={pf:.2f} trades={n_tr} [{psum}] | {c['rationale_preview']}"
            )
        lines.append("")

        # --- Duplicate-family signatures across actives -------------------
        groups: dict[tuple, list[int]] = defaultdict(list)
        for r in active_rows:
            fp = _param_fingerprint(r["params_json"])
            key = (r["symbol"], r["strategy_family"], fp)
            groups[key].append(int(r["id"]))
        dup_lines: list[str] = []
        for (sym, fam, fp), ids in groups.items():
            if len(ids) > 1:
                fp_str = ",".join(f"{k}={v}" for k, v in fp) if fp else "(no-fp-keys)"
                dup_lines.append(
                    f"  {sym} {fam} [{fp_str}] → ids {ids} (consider retiring all but best)"
                )
        if dup_lines:
            lines.append("DUPLICATE-FAMILY SIGNATURES across actives:")
            lines.extend(dup_lines)
            lines.append("")
        else:
            lines.append("DUPLICATE-FAMILY SIGNATURES: none detected.\n")

        conn.close()
    except Exception:
        log.exception("reviewer._build_kickoff failed")

    # Burnout summary — show which symbols have been repeatedly retired
    try:
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        burnout_rows = conn2.execute("""
            SELECT symbol, COUNT(*) as n_retired
            FROM auto_demotions
            WHERE ts_utc > datetime('now', '-7 days')
            GROUP BY symbol HAVING n_retired >= 2
            ORDER BY n_retired DESC
        """).fetchall()
        if burnout_rows:
            lines.append("BURNOUT SUMMARY (symbols with repeated retirements in 7 days):")
            for b in burnout_rows:
                lines.append(f"  {b['symbol']}: {b['n_retired']} retirements — structurally difficult")
            lines.append(
                "  NOTE: An empty slot is SAFER than a net-loser strategy. "
                "Do NOT promote weak candidates just to fill coverage."
            )
            lines.append("")
        conn2.close()
    except Exception:
        pass

    lines.append(
        "REVIEWER PRINCIPLES:\n"
        "- Cost-adjusted (*_net) metrics are the source of truth — use them "
        "when present.\n"
        "- Active strategies with pf_20_net < 0.9 on a real sample are retire "
        "candidates.\n"
        "- Candidate promotion requires ALL: net PF >= 1.3, E[R] > 0, "
        "total_test_trades >= 20. No exception for uncovered symbols — "
        "empty slot beats net-loser.\n"
        "- Duplicates: same (symbol, family, execution_engine, strategy_type) "
        "with near-identical numeric params — retire all but highest PF.\n"
        "- Newly promoted strategies (active < 14 days) should NOT be retired "
        "for staleness. Only retire stale strategies that have been active > 14d.\n"
        "- Use record_finding to capture surprises (e.g. a single param "
        "tweak that flipped PF positive).\n"
    )
    lines.append(
        "Your job: drain the candidate queue and prune the actives. Act now."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_reviewer_decision(
    *, db_path: str, turn_budget: int = 0, time_budget_s: int = 0,
    minimax_model: str = "minimax/MiniMax-M3",
) -> AgentDecisionResult:
    """Run one Reviewer cycle."""
    started = time.time()
    if os.environ.get("JUDAS_REVIEWER_AGENT_INHIBIT") == "1":
        log.info("reviewer_agent.inhibited_by_env")
        return AgentDecisionResult(
            success=True, actions_taken=[],
            narrative="Reviewer inhibited via JUDAS_REVIEWER_AGENT_INHIBIT.",
            turns_used=0, elapsed_s=time.time() - started,
            fallback_used=True, raw_messages=[], error=None,
        )

    tools, schemas = agent_tools.make_tools(
        db_path=db_path, include=INCLUDE_TOOLS, team="reviewer",
        claimed_by="reviewer_agent", author="reviewer",
    )
    return run_agent_loop(
        db_path=db_path,
        system_prompt=SYSTEM_PROMPT.format(
            turn_budget=turn_budget, time_budget_s=time_budget_s,
        ),
        user_kickoff=_build_reviewer_kickoff(db_path),
        tools=tools, schemas=schemas,
        turn_budget=turn_budget, time_budget_s=time_budget_s,
        minimax_model=minimax_model,
    )
