"""Self-audit block for agent kickoff briefings.

Surfaces patterns that the agents already have the tools to fix, but
have been blind to because the briefings only show flat lists of
strategies / candidates / trades — not the *patterns* that explain why
the lab is losing money.

The goal is observability, not enforcement. The operator, registrar,
and researcher each have their own action set (retire_strategy,
modify_strategy_params, propose_candidate, ...) and decide for
themselves what to do with what they see here.

Computed cheap from SQLite each cycle — no caching, no LLM, runs in
well under a second.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict


_JUDAS_NATIVE_RUNTIME_KEYS = (
    "detector_lookback_bars", "confirmation_bars", "pivot_length",
    "target_r", "stop_buffer_ticks", "min_sweep_ticks",
)


def build_leaderboard_block(db_path: str) -> str:
    """Rich scoreboard for operator/reviewer/researcher kickoffs.

    Uses src/research/leaderboard_stats.py for the underlying data so
    the dashboard, the agent's get_leaderboard tool, and this kickoff
    block all share one source of truth.
    """
    out: list[str] = ["=== LEADERBOARD (the scoreboard — your truth source) ==="]
    try:
        from src.research import leaderboard_stats as ls
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        sleeve = ls.sleeve_summary(conn)
        out.append(
            f"SLEEVE: ${sleeve['starting']:,.0f} → ${sleeve['current']:,.2f} "
            f"({sleeve['growth_pct']:+.2f}%)  |  "
            f"trades closed: {sleeve['trades_closed']} "
            f"(real-fill {sleeve['trades_real_fill']}, "
            f"real-fill pnl ${sleeve['pnl_real_fill']:+,.2f})  |  "
            f"archived (pre-reset): {sleeve['archived_trades']} trades "
            f"${sleeve['archived_pnl']:+,.2f}"
        )
        out.append("")

        # REAL WINNING TRADES, by symbol — survives strategy retirement.
        # A symbol can be net-profitable even if the strategy that earned it
        # was since retired/replaced; this keeps the win visible so agents
        # don't retire/ignore a productive symbol. (Requested 2026-05-30.)
        winners = ls.winners_by_symbol(conn)
        if winners:
            out.append("REAL CLOSED TRADES (by symbol — money actually made; persists past retirement):")
            for w in winners:
                tag = "🟢" if w["net_pnl"] > 0 else ("🔴" if w["net_pnl"] < 0 else "  ")
                out.append(
                    f"  {tag} {w['symbol']:<4} net ${w['net_pnl']:+9.2f} | "
                    f"{w['wins']}W/{w['losses']}L ({w['winrate']:.0f}% WR) | "
                    f"best ${w['best_trade']:+.2f} worst ${w['worst_trade']:+.2f} | "
                    f"last {str(w['last_close_utc'])[:10]}"
                )
            out.append("")

        cov = ls.coverage_summary(conn)
        out.append(
            f"COVERAGE: {cov['total_active_symbols']}/{cov['total_target_symbols']} "
            f"symbols — covered={cov['covered']}"
        )
        if cov["uncovered"]:
            out.append(
                f"  UNCOVERED → researcher PRIORITY (use non-Judas families): "
                f"{cov['uncovered']}"
            )
        out.append("")

        stats = ls.all_active_stats(conn, limit=1000)  # show ALL actives, not a slice
        if stats:
            out.append("ACTIVE STRATEGIES (rich stats; sorted cost-aware → live PF → BT PF):")
            out.append(
                "  " + " | ".join([
                    f"{'id':>5}", f"{'sym':<4}", f"{'engine':<14}", f"{'fam':<22}",
                    f"{'BT_PF':>6}", f"{'BT_n':>5}", f"{'BT_E[R]':>8}",
                    f"{'LIVE_PF':>8}", f"{'LIVE_n':>7}", f"{'WR':>5}",
                    f"{'NET_PnL':>10}", f"{'EXP$/T':>8}",
                    f"{'AGE_d':>6}", f"{'STALE_d':>8}",
                    "FLAGS",
                ])
            )
            for r in stats:  # every active strategy — full visibility, not top-20
                flags: list[str] = []
                if r["gap_flag"]: flags.append("⚠GAP")
                if r.get("no_backtest_metrics"): flags.append("⚠NO_BT")
                if r["unproven_live"]: flags.append("UNPROVEN")
                if r["stale"]: flags.append("STALE")
                if r["displacement_r_bug"]: flags.append("⚠DISPL_R_BUG")
                if not r["cost_model"]: flags.append("PRE-COST")
                bt_pf = f"{r['backtest_pf']:6.2f}" if r["backtest_pf"] is not None else "   n/a"
                bt_er = f"{r['backtest_expectancy_r']:+8.2f}" if r["backtest_expectancy_r"] is not None else "     n/a"
                lpf = f"{r['live_pf_net']:7.2f}" if r["live_pf_net"] is not None else "    n/a"
                wr = f"{(r['live_winrate'] or 0)*100:4.0f}%" if r["live_winrate"] is not None else "  n/a"
                age = f"{r['age_days']:5d}" if r["age_days"] is not None else "  n/a"
                stale = f"{r['days_since_last_fire']:7d}" if r["days_since_last_fire"] is not None else "    n/a"
                out.append(
                    "  " + " | ".join([
                        f"{r['id']:>5}",
                        f"{r['symbol']:<4}",
                        f"{(r['execution_engine'] or '?')[:14]:<14}",
                        f"{r['strategy_family'][:22]:<22}",
                        bt_pf,
                        f"{r['backtest_n']:>5}",
                        bt_er,
                        lpf,
                        f"{r['live_n_real']:>3}/{r['live_n_total']:<3}",
                        wr,
                        f"${r['live_pnl_net']:>9.2f}",
                        f"${r['expectancy_dollars']:>7.2f}",
                        age,
                        stale,
                        " ".join(flags),
                    ])
                )
            out.append("")
            out.append("  Column key:")
            out.append("    BT_PF/BT_n/BT_E[R] = backtest walk-forward profit factor, trade count, expectancy in R units")
            out.append("    LIVE_PF/LIVE_n     = net live profit factor / (real-fill trades / total trades)")
            out.append("    WR / NET_PnL       = live win rate / net-of-cost dollar P&L this strategy")
            out.append("    EXP$/T             = average dollars per trade (expectancy)")
            out.append("    AGE_d / STALE_d    = days since activated / days since last fire")
            out.append("    Flags: ⚠GAP=backtest lied,investigate  UNPROVEN=live n<3  STALE=>7d no fire")
            out.append("           ⚠NO_BT=no backtest metrics — promoted blind, RE-BACKTEST URGENT")
            out.append("           ⚠DISPL_R_BUG=judas_native ignores displacement_r param")
            out.append("           PRE-COST=metrics computed before cost_model v1")

        wf = ls.recent_walk_forward_candidates(conn, limit=10)
        if wf:
            out.append("")
            out.append("RECENT WALK-FORWARDS (promotion candidates, top by net PF):")
            out.append(f"  {'exp_id':>6} | {'sym':<4} | {'name':<32} | {'PF':>5} | {'n':>4} | {'cost'}")
            for c in wf:
                cm_tag = c.get("cost_model") or "—"
                out.append(
                    f"  {c['experiment_id']:>6} | {c['symbol']:<4} | "
                    f"{c['name'][:32]:<32} | {c['backtest_pf']:5.2f} | "
                    f"{c['backtest_n']:>4} | {cm_tag}"
                )

        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append(f"[leaderboard_block error: {exc}]")

    # --- Workshop cross-reference: what's actually working in the
    # non-agentic sibling repo. Use this as a seed source for what
    # strategy families to bring into the agentic crew.
    # NB: knowledge_base/buffet.yaml is a STATIC backtest file (no live paper
    # trade data). Keys actually present per row: id, symbol, kind, type,
    # pf, trades, params. live-paper columns (paper_n, paper_pnl_dollars,
    # fires_total) are NOT in this file — drop them rather than KeyError.
    try:
        from src.research.explore import _load_workshop_leaderboard_compact
        wb = _load_workshop_leaderboard_compact(limit=20)
        # Sort by pf desc.
        wb_sorted = sorted(wb, key=lambda r: -float(r.get("pf") or 0.0))
        if wb_sorted:
            out.append("")
            out.append("=== WORKSHOP CROSS-REFERENCE (the non-agentic system's Strategy Buffet) ===")
            out.append("Static backtest file at /home/rob/judas-futures-workshop")
            out.append("→ knowledge_base/buffet.yaml. Use as a seed source for new strategy families.")
            out.append("Live paper-trade data not currently exported — this block is backtest-only.")
            out.append("")
            out.append(
                f"  {'strategy_id':<30} {'sym':<7} {'kind':<6} {'BT_PF':>6} "
                f"{'BT_n':>5} {'type':<10}"
            )
            for r in wb_sorted[:12]:
                out.append(
                    f"  {str(r['id'])[:30]:<30} {str(r['symbol'])[:7]:<7} "
                    f"{str(r['kind'])[:6]:<6} {float(r.get('pf') or 0):6.2f} "
                    f"{int(r.get('trades') or 0):>5} {str(r.get('type') or '')[:10]:<10}"
                )
            out.append("")
            out.append("  Use get_workshop_leaderboard(limit=N) for the full ranked list.")
    except Exception as exc:  # noqa: BLE001
        out.append(f"[workshop_block error: {exc}]")

    out.append("=== END LEADERBOARD ===")
    return "\n".join(out)


def _build_leaderboard_block_legacy(db_path: str) -> str:
    """Kept for reference — the inline computation version before
    leaderboard_stats.py existed. Not called anywhere; remove later.
    """
    out: list[str] = ["=== LEADERBOARD + LIVE GAP (the scoreboard) ==="]
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, symbol, strategy_family, version, params_json, metrics_json
            FROM active_strategies WHERE state='active'
            """
        ).fetchall()
        if not rows:
            out.append("  (no active strategies)")
        active_data: list[dict] = []
        for r in rows:
            try:
                params = json.loads(r["params_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                params = {}
            try:
                metrics = json.loads(r["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            sid = int(r["id"])
            live = conn.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(pnl_dollars), 0) AS pnl,
                       COALESCE(SUM(CASE WHEN pnl_dollars>0 THEN pnl_dollars ELSE 0 END), 0) AS gw,
                       COALESCE(SUM(CASE WHEN pnl_dollars<0 THEN -pnl_dollars ELSE 0 END), 0) AS gl,
                       MAX(opened_at) AS last_fire
                FROM trades WHERE strategy_id=? AND status='closed'
                """,
                (sid,),
            ).fetchone()
            live_n = int(live["n"] or 0)
            gw, gl = float(live["gw"] or 0), float(live["gl"] or 0)
            live_pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
            bt_pf = float(metrics.get("profit_factor")
                          or metrics.get("avg_test_profit_factor")
                          or metrics.get("pf_20") or 0)
            bt_n = int(metrics.get("trades")
                       or metrics.get("total_test_trades") or 0)
            cm = params.get("cost_model") or metrics.get("cost_model")
            unproven = (bt_pf > 0 and live_n < 3)
            gap = (bt_pf >= 2.0 and live_n >= 3 and live_pf <= 0)
            active_data.append({
                "id": sid, "symbol": r["symbol"],
                "family": r["strategy_family"], "version": r["version"],
                "name": params.get("strategy_name") or "?",
                "bt_pf": bt_pf, "bt_n": bt_n,
                "live_pf": live_pf if live_n >= 3 else None,
                "live_n": live_n, "live_pnl": float(live["pnl"] or 0),
                "last_fire": live["last_fire"],
                "cost_model": cm,
                "unproven": unproven, "gap": gap,
            })
        active_data.sort(key=lambda r: (
            0 if r["cost_model"] else 1,
            -(r["live_pf"] if r["live_pf"] is not None else -1e9),
            -r["bt_pf"],
        ))
        if active_data:
            out.append("ACTIVE STRATEGIES (cost-aware rows first; sorted by live PF, then backtest PF):")
            out.append(f"  {'id':>5} {'sym':<4} {'fam':<22} BT_PF  BT_n  LIVE_PF  LIVE_n  LIVE_$    FLAGS")
            for r in active_data[:20]:
                lpf = f"{r['live_pf']:6.2f}" if r['live_pf'] is not None else "  n/a "
                flags = []
                if r["gap"]: flags.append("⚠GAP")
                if r["unproven"]: flags.append("UNPROVEN")
                if not r["cost_model"]: flags.append("PRE-COST")
                out.append(
                    f"  {r['id']:>5} {r['symbol']:<4} {r['family']:<22} "
                    f"{r['bt_pf']:5.2f} {r['bt_n']:5}  {lpf}  {r['live_n']:>5}  "
                    f"${r['live_pnl']:+8.2f}  {' '.join(flags)}"
                )

        # Coverage
        all_syms = {"MGC", "MNQ", "MCL", "MBT", "MET", "DX", "ZF", "6J"}
        covered = {r["symbol"] for r in active_data}
        uncovered = sorted(all_syms - covered)
        out.append("")
        out.append(f"COVERAGE: {len(covered)}/8 symbols — covered={sorted(covered)}")
        if uncovered:
            out.append(f"  UNCOVERED → researcher priority: {uncovered}")

        # Top backtest walk-forwards not yet active (promotion candidates)
        wf_rows = conn.execute(
            """
            SELECT id, ts_utc, symbol, name, metrics_json
            FROM research_experiments
            WHERE experiment_type='walk_forward'
            ORDER BY id DESC LIMIT 80
            """
        ).fetchall()
        wf_candidates: list[dict] = []
        for wf in wf_rows:
            try:
                m = json.loads(wf["metrics_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            pf = m.get("avg_test_profit_factor")
            n_trades = m.get("total_test_trades")
            if pf is None or n_trades is None:
                continue
            try:
                pf_f = float(pf); n_f = int(n_trades)
            except (TypeError, ValueError):
                continue
            wf_candidates.append({
                "id": int(wf["id"]), "symbol": str(wf["symbol"]),
                "name": str(wf["name"])[:30], "pf": pf_f, "n": n_f,
                "cm": m.get("cost_model"),
            })
        wf_candidates.sort(key=lambda r: (0 if r["cm"] else 1, -r["pf"]))
        if wf_candidates:
            out.append("")
            out.append("TOP RECENT WALK-FORWARDS (promotion candidates):")
            for c in wf_candidates[:8]:
                cm_tag = "" if c["cm"] else " [pre-cost]"
                out.append(
                    f"  #{c['id']:>5} {c['symbol']:<4} {c['name']:<30} "
                    f"PF={c['pf']:5.2f} n={c['n']:3}{cm_tag}"
                )

        # Sleeve P&L summary
        sleeve = conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(pnl_dollars), 0) AS total_pnl,
                   COALESCE(SUM(CASE WHEN exit_reason LIKE '%_real' THEN pnl_dollars ELSE 0 END), 0) AS real_pnl,
                   COUNT(CASE WHEN exit_reason LIKE '%_real' THEN 1 END) AS real_n
            FROM trades WHERE status='closed'
            """
        ).fetchone()
        starting = 5000.0
        total_pnl = float(sleeve["total_pnl"] or 0)
        real_pnl = float(sleeve["real_pnl"] or 0)
        out.append("")
        out.append(
            f"SLEEVE: starting ${starting:,.0f}  →  current ${starting + total_pnl:,.2f}  "
            f"(total_pnl ${total_pnl:+,.2f} across {sleeve['n']} closed trades; "
            f"${real_pnl:+,.2f} on {sleeve['real_n']} real-fill trades)"
        )

        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append(f"[leaderboard_block error: {exc}]")
    out.append("=== END LEADERBOARD ===")
    return "\n".join(out)


def build_self_audit(db_path: str) -> str:
    """Return a multi-line markdown-ish audit block.

    Caller splices this into the agent kickoff. Failures degrade
    gracefully — if any section throws, it's omitted with a comment.
    """
    out: list[str] = ["=== SELF-AUDIT (last 7 days unless noted) ==="]

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        out.extend(_section_pnl_by_setup(conn))
        out.extend(_section_duplicate_fires(conn))
        out.extend(_section_zombie_actives(conn))
        out.extend(_section_demotion_reasons(conn))
        out.extend(_section_stuck_queue(conn))

        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append(f"[self_audit error: {exc}]")

    out.append("=== END SELF-AUDIT ===")
    return "\n".join(out)


def _section_pnl_by_setup(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT symbol, strategy_family,
               COUNT(*) AS n,
               SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN pnl_dollars < 0 THEN 1 ELSE 0 END) AS losses,
               ROUND(SUM(pnl_dollars), 2) AS pnl
        FROM trades
        WHERE status='closed'
          AND opened_at >= datetime('now', '-7 days')
        GROUP BY symbol, strategy_family
        ORDER BY pnl ASC
        """
    ).fetchall()
    if not rows:
        return ["", "PNL BY SETUP: no closed trades in last 7 days.", ""]
    lines = ["", "PNL BY SETUP (sorted worst first):"]
    total = 0.0
    total_n = 0
    total_w = 0
    for r in rows:
        wr = (int(r["wins"]) / int(r["n"]) * 100.0) if int(r["n"]) else 0.0
        lines.append(
            f"  {r['symbol']:>4} {r['strategy_family']:<24} "
            f"n={int(r['n']):>3} W/L={int(r['wins'])}/{int(r['losses'])} "
            f"WR={wr:4.0f}%  PnL=${float(r['pnl'] or 0):+.2f}"
        )
        total += float(r["pnl"] or 0)
        total_n += int(r["n"])
        total_w += int(r["wins"])
    overall_wr = (total_w / total_n * 100.0) if total_n else 0.0
    lines.append(f"  TOTAL: n={total_n} WR={overall_wr:.0f}% PnL=${total:+.2f}")
    if overall_wr == 0 and total < 0:
        lines.append("  ⚠  WR=0 over a real sample — every active loss-leader is a candidate for retire_strategy.")
    lines.append("")
    return lines


def _section_duplicate_fires(conn: sqlite3.Connection) -> list[str]:
    """Find groups of trades on the same (symbol, direction) within 60s of
    each other. Each group is evidence that N active versions fired the
    same setup — they're not diversifying, they're stacking the same bet
    and each takes the same stop. Use modify_strategy_params or
    retire_strategy to converge them.
    """
    rows = conn.execute(
        """
        SELECT id, symbol, direction, opened_at, strategy_id, strategy_family,
               strategy_version, pnl_dollars
        FROM trades
        WHERE opened_at >= datetime('now', '-14 days')
        ORDER BY symbol, direction, opened_at
        """
    ).fetchall()
    if not rows:
        return []

    groups: list[list[sqlite3.Row]] = []
    current: list[sqlite3.Row] = []
    last_key: tuple[str, str] | None = None
    last_ts: str | None = None
    for r in rows:
        key = (str(r["symbol"]), str(r["direction"]))
        ts = str(r["opened_at"])
        if (
            last_key == key
            and last_ts is not None
            and _seconds_between(last_ts, ts) <= 60
        ):
            current.append(r)
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [r]
            last_key = key
        last_ts = ts
    if len(current) >= 2:
        groups.append(current)

    if not groups:
        return ["DUPLICATE-FIRE GROUPS: none detected in last 14d.", ""]
    lines = ["DUPLICATE-FIRE GROUPS (≥2 trades on same symbol/direction within 60s):"]
    for g in groups[-10:]:
        n = len(g)
        sym = g[0]["symbol"]
        direction = g[0]["direction"]
        ts = g[0]["opened_at"]
        fam = g[0]["strategy_family"]
        versions = sorted({int(t["strategy_version"]) for t in g})
        pnl_sum = sum(float(t["pnl_dollars"] or 0) for t in g)
        lines.append(
            f"  {ts}  {sym} {direction} {fam}  versions={versions}  "
            f"n={n}  cumulative PnL=${pnl_sum:+.2f}"
        )
    lines.append(
        "  → Same setup fanned across N active versions. Use retire_strategy or "
        "modify_strategy_params to converge — multiple versions are only useful if "
        "they enter on DIFFERENT setups."
    )
    lines.append("")
    return lines


def _section_zombie_actives(conn: sqlite3.Connection) -> list[str]:
    """Active judas_native strategies whose params include none of the
    keys the runtime actually reads. They run on default detector params
    — so a (symbol, family) group of them all fires the same trade on
    every bar.
    """
    rows = conn.execute(
        """
        SELECT id, symbol, strategy_family, version, params_json, activated_at_utc
        FROM active_strategies
        WHERE state='active'
        """
    ).fetchall()
    zombies: list[sqlite3.Row] = []
    by_sym_fam: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        try:
            p = json.loads(r["params_json"] or "{}")
        except (TypeError, ValueError):
            p = {}
        engine = str(p.get("execution_engine", "judas_native")).lower()
        if engine != "judas_native":
            continue
        if not any(k in p for k in _JUDAS_NATIVE_RUNTIME_KEYS):
            zombies.append(r)
            by_sym_fam[(str(r["symbol"]), str(r["strategy_family"]))] += 1
    if not zombies:
        return ["ZOMBIE ACTIVES (judas_native, no runtime keys): none.", ""]
    lines = [
        f"ZOMBIE ACTIVES ({len(zombies)} rows) — judas_native strategies whose "
        "params have NONE of:",
        f"  {', '.join(_JUDAS_NATIVE_RUNTIME_KEYS)}",
        "  These promote successfully but the detector falls back to defaults, so "
        "every such version on the same (symbol, family) fires the SAME trade.",
        "  Concentration by (symbol, family):",
    ]
    for (sym, fam), n in sorted(by_sym_fam.items(), key=lambda kv: -kv[1]):
        lines.append(f"    {sym:>4} {fam:<24} {n} active rows")
    sample_ids = [int(r["id"]) for r in zombies[:12]]
    lines.append(f"  Sample ids to inspect/retire: {sample_ids}")
    lines.append("")
    return lines


def _section_demotion_reasons(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT substr(reason, 1, 60) AS short_reason, COUNT(*) AS n
        FROM auto_demotions
        WHERE ts_utc >= datetime('now', '-14 days')
        GROUP BY short_reason
        ORDER BY n DESC LIMIT 6
        """
    ).fetchall()
    if not rows:
        return []
    lines = ["TOP AUTO-DEMOTION REASONS (last 14d):"]
    for r in rows:
        lines.append(f"  {int(r['n']):>4}× — {r['short_reason']}")
    lines.append(
        "  → If 'broken params' or similar dominates, the upstream researcher/"
        "registrar pipeline is producing strategies the runtime can't execute. "
        "Inspect a sample candidate's params_json vs portfolio_runtime keys."
    )
    lines.append("")
    return lines


def _section_stuck_queue(conn: sqlite3.Connection) -> list[str]:
    by_team_action = conn.execute(
        """
        SELECT team, action, COUNT(*) AS n,
               MIN(requested_at_utc) AS oldest
        FROM agent_tasks
        WHERE status='open'
        GROUP BY team, action
        ORDER BY n DESC LIMIT 8
        """
    ).fetchall()
    if not by_team_action:
        return []
    lines = ["STUCK TASK QUEUE (open, by team/action):"]
    for r in by_team_action:
        lines.append(
            f"  {r['team']:<10} {r['action']:<24} n={int(r['n']):>3}  oldest={r['oldest']}"
        )
    lines.append("")
    return lines


def _seconds_between(ts_a: str, ts_b: str) -> float:
    from datetime import datetime

    def _parse(t: str) -> datetime:
        t = t.replace("Z", "+00:00").replace(" ", "T")
        return datetime.fromisoformat(t)

    try:
        return abs((_parse(ts_b) - _parse(ts_a)).total_seconds())
    except Exception:  # noqa: BLE001
        return 10**9
