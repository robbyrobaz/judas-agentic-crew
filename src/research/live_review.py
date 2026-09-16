"""Live-performance review for Phase 2 of the Agentic Operator Plan.

Computes rolling performance metrics for each active strategy, then decides
keep / retune / retire. Has a deterministic threshold fallback that is the
source of truth when the LLM path is unavailable or fails.

Public surface:
    - StrategyMetrics dataclass
    - Decision dataclass
    - compute_live_metrics(db_path=..., strategy_id=...)
    - decide_action(metrics, regime=..., leaderboard=...)
    - review_all_active_strategies(db_path=...)
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.db.models import get_conn

log = logging.getLogger(__name__)

_LLM_TIMEOUT_S = 30
_ROLLING_WINDOW = 20


@dataclass
class StrategyMetrics:
    strategy_id: int
    strategy_name: str
    n_closed_trades: int
    pf_20: float
    expectancy_20: float
    max_consec_losers: int
    days_since_last_fire: int | None
    total_realized_pnl: float


@dataclass
class Decision:
    action: str  # "keep" | "retune" | "retire"
    reason: str
    confidence: float
    fallback_used: bool


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def _parse_iso_utc(ts: str) -> datetime | None:
    """Parse ISO-8601 timestamp; tolerate trailing 'Z'."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _strategy_name(row: Any) -> str:
    """Build a human-readable name for a strategy row."""
    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    name = params.get("strategy_name")
    if name:
        return str(name)
    return f"{row['symbol']}/{row['strategy_family']}/v{row['version']}"


def compute_live_metrics(
    *, db_path: str, strategy_id: int, real_fills_only: bool = False
) -> StrategyMetrics:
    """Compute rolling-20 metrics for ``strategy_id`` from the trades table.

    Returns ``StrategyMetrics``. For < 5 closed trades, pf_20 / expectancy_20
    are NaN. Caller decides what to do with sparse data.
    """
    nan = float("nan")
    with get_conn(db_path) as conn:
        active = conn.execute(
            "SELECT * FROM active_strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if active is None:
            raise ValueError(f"strategy_id {strategy_id} not found")
        name = _strategy_name(active)

        # Closed trades for this strategy, most recent first.
        real_filter = " AND exit_reason LIKE '%_real'" if real_fills_only else ""
        closed = conn.execute(
            f"""
            SELECT pnl_dollars, opened_at, closed_at
            FROM trades
            WHERE strategy_id = ? AND status = 'closed'
            {real_filter}
            ORDER BY datetime(COALESCE(closed_at, opened_at)) DESC
            """,
            (strategy_id,),
        ).fetchall()

        # Last fire (any trade row, even open) for staleness.
        last_fire_row = conn.execute(
            """
            SELECT opened_at
            FROM trades
            WHERE strategy_id = ?
            ORDER BY datetime(opened_at) DESC
            LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()

    n_closed = len(closed)
    pnls = [float(r["pnl_dollars"]) for r in closed if r["pnl_dollars"] is not None]
    total_pnl = float(sum(pnls))

    days_since_last_fire: int | None
    if last_fire_row and last_fire_row["opened_at"]:
        last_dt = _parse_iso_utc(str(last_fire_row["opened_at"]))
        if last_dt is None:
            days_since_last_fire = None
        else:
            now = datetime.now(timezone.utc)
            days_since_last_fire = max(0, (now - last_dt).days)
    else:
        days_since_last_fire = None

    if n_closed < 5:
        return StrategyMetrics(
            strategy_id=strategy_id,
            strategy_name=name,
            n_closed_trades=n_closed,
            pf_20=nan,
            expectancy_20=nan,
            max_consec_losers=_max_consec_losers(pnls),
            days_since_last_fire=days_since_last_fire,
            total_realized_pnl=total_pnl,
        )

    window = pnls[: min(_ROLLING_WINDOW, n_closed)]
    gross_profit = sum(p for p in window if p > 0)
    gross_loss = -sum(p for p in window if p < 0)
    if gross_loss == 0:
        # All wins (or zero) — treat as very strong PF.
        pf_20 = math.inf if gross_profit > 0 else 1.0
    else:
        pf_20 = gross_profit / gross_loss

    # Expectancy in R: approximate using mean(pnl) / mean(|loss|).
    losses = [abs(p) for p in window if p < 0]
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    if avg_loss > 0:
        expectancy_20 = (sum(window) / len(window)) / avg_loss
    else:
        expectancy_20 = float("inf") if sum(window) > 0 else 0.0

    return StrategyMetrics(
        strategy_id=strategy_id,
        strategy_name=name,
        n_closed_trades=n_closed,
        pf_20=float(pf_20),
        expectancy_20=float(expectancy_20),
        max_consec_losers=_max_consec_losers(pnls),
        days_since_last_fire=days_since_last_fire,
        total_realized_pnl=total_pnl,
    )


def _max_consec_losers(pnls_recent_first: list[float]) -> int:
    """Longest run of losers anywhere in the series."""
    best = 0
    cur = 0
    for p in pnls_recent_first:
        if p < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _deterministic_decide(metrics: StrategyMetrics) -> Decision:
    """Deterministic fallback. Only fires when the LLM path is unavailable.

    Design note (revised 2026-05-10 after operator feedback):
      The previous version protected `n_closed_trades < 5` strategies with
      "keep — insufficient data". That was wrong. Workshop-seeded strategies
      aren't sacred — demotions are cheap (one-click reactivate via
      auto_demotions), promotions are gated. The system should bias toward
      retire + easy rollback, not toward protecting stale slots in the
      active set. This rule now mirrors the reasoning M3 would do: a
      strategy with no closed trades AND no recent fire activity is a
      candidate for retire, not a candidate for indefinite protection.
    """
    pf = metrics.pf_20
    pf_finite = not (pf is None or math.isnan(pf))

    # Performance-based retire (highest confidence)
    if (pf_finite and pf < 0.9) or metrics.max_consec_losers >= 6:
        return Decision(
            action="retire",
            reason=(
                f"pf_20={pf:.2f} max_consec_losers={metrics.max_consec_losers} "
                "below retire threshold"
            ),
            confidence=0.9,
            fallback_used=True,
        )

    # Inactive slot — never fired or stale. Retire and let it reactivate
    # via the dashboard if the operator disagrees.
    if metrics.n_closed_trades == 0:
        days = metrics.days_since_last_fire
        if days is None or days >= 7:
            return Decision(
                action="retire",
                reason=(
                    f"inactive: 0 closed trades, days_since_last_fire={days}. "
                    "Retire is cheap; reactivate via auto_demotions if needed."
                ),
                confidence=0.7,
                fallback_used=True,
            )

    # Stale + marginal → retune
    stale = (metrics.days_since_last_fire or 0) >= 14
    if pf_finite and pf < 1.1 and stale:
        return Decision(
            action="retune",
            reason=(
                f"stale + marginal: pf_20={pf:.2f} "
                f"days_since_last_fire={metrics.days_since_last_fire}"
            ),
            confidence=0.7,
            fallback_used=True,
        )

    # Healthy or recently fired with too few trades to judge — keep watching.
    return Decision(
        action="keep",
        reason=(
            f"healthy: pf_20={pf} n_closed_trades={metrics.n_closed_trades}"
            if pf_finite
            else f"observing: n_closed_trades={metrics.n_closed_trades}"
        ),
        confidence=0.6,
        fallback_used=True,
    )


def _llm_decide(
    metrics: StrategyMetrics,
    *,
    regime: dict | None,
    leaderboard: list | None,
) -> Decision | None:
    """Try the M3 LLM path. Return None on any failure (caller falls back).

    Implementation note: kept thin so tests can monkeypatch ``_call_llm``.
    """
    try:
        prompt = _build_llm_prompt(metrics, regime=regime, leaderboard=leaderboard)
        raw = _call_llm(prompt)
        parsed = _parse_llm_response(raw)
        if parsed is None:
            return None
        return Decision(
            action=parsed["action"],
            reason=str(parsed.get("reason", "")),
            confidence=float(parsed.get("confidence", 0.6)),
            fallback_used=False,
        )
    except Exception as exc:  # noqa: BLE001 — defensive on the LLM path
        log.warning(
            "live_review.llm.failed",
            extra={"strategy_id": metrics.strategy_id, "error": str(exc)},
        )
        return None


def _build_llm_prompt(
    metrics: StrategyMetrics,
    *,
    regime: dict | None,
    leaderboard: list | None,
) -> str:
    top5 = (leaderboard or [])[:5]
    payload = {
        "metrics": metrics.__dict__,
        "regime": regime or {},
        "leaderboard_top5": top5,
    }
    return (
        "You are a paper-trading strategy reviewer. Given the live metrics, "
        "regime tags, and current leaderboard, decide one of: keep, retune, "
        "retire. Respond ONLY with a JSON object of the shape "
        '{"action": "keep|retune|retire", "reason": "<short>", '
        '"confidence": <0..1 float>}.\n\n'
        f"Input: {json.dumps(payload, default=str)}"
    )


def _call_llm(prompt: str) -> str:
    """Invoke MiniMax M3 via litellm. Caller wraps in try/except."""
    if not os.environ.get("MINIMAX_API_KEY"):
        raise RuntimeError("MINIMAX_API_KEY not set")

    import litellm  # type: ignore[import-not-found]

    resp = litellm.completion(
        model="minimax/MiniMax-M3",
        messages=[{"role": "user", "content": prompt}],
        timeout=_LLM_TIMEOUT_S,
        temperature=0.0,
    )
    return resp["choices"][0]["message"]["content"]  # type: ignore[index]


def _parse_llm_response(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    # Strip code fences if present.
    if text.startswith("```"):
        text = text.strip("`")
        # remove leading "json\n" if present
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to grab the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    if action not in {"keep", "retune", "retire"}:
        return None
    return data


def decide_action(
    metrics: StrategyMetrics,
    *,
    regime: dict | None = None,
    leaderboard: list | None = None,
) -> Decision:
    """Decide keep / retune / retire for a strategy.

    Prefers the M3 LLM path when ``MINIMAX_API_KEY`` is set; falls back to
    deterministic thresholds on any failure or when the env var is missing.
    """
    if os.environ.get("MINIMAX_API_KEY"):
        decision = _llm_decide(metrics, regime=regime, leaderboard=leaderboard)
        if decision is not None:
            return decision
        # Fall through to deterministic — mark fallback.
        det = _deterministic_decide(metrics)
        return Decision(
            action=det.action,
            reason=det.reason + " (llm fallback)",
            confidence=det.confidence,
            fallback_used=True,
        )
    return _deterministic_decide(metrics)


# ---------------------------------------------------------------------------
# Bulk review
# ---------------------------------------------------------------------------


def metrics_to_dict(m: StrategyMetrics) -> dict:
    """JSON-safe view of StrategyMetrics (NaN/inf → None)."""
    def _f(v):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return v
        return None if (math.isnan(fv) or math.isinf(fv)) else round(fv, 4)
    return {
        "strategy_id": m.strategy_id,
        "strategy_name": m.strategy_name,
        "n_closed_trades": m.n_closed_trades,
        "pf_20": _f(m.pf_20),
        "expectancy_20": _f(m.expectancy_20),
        "max_consec_losers": m.max_consec_losers,
        "days_since_last_fire": m.days_since_last_fire,
        "total_realized_pnl": _f(m.total_realized_pnl),
    }


def review_all_active_deterministic(
    *, db_path: str
) -> list[tuple[StrategyMetrics, Decision, int]]:
    """Live metrics + the deterministic rule verdict for every active row.

    No LLM calls. Returns (metrics, decision, activated_days_ago). This is the
    advisory input for the reviewer's briefing (2026-09-16): until then the
    rules in _deterministic_decide had zero callers and the reviewer judged
    strategies on the backtest blob frozen in metrics_json."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, activated_at_utc FROM active_strategies "
            "WHERE state = 'active' ORDER BY id"
        ).fetchall()
    out: list[tuple[StrategyMetrics, Decision, int]] = []
    now = datetime.now(timezone.utc)
    for row in rows:
        sid = int(row["id"])
        try:
            metrics = compute_live_metrics(db_path=db_path, strategy_id=sid)
            decision = _deterministic_decide(metrics)
            act = _parse_iso_utc(str(row["activated_at_utc"] or ""))
            age = (now - act).days if act else 0
            out.append((metrics, decision, age))
        except Exception as exc:  # noqa: BLE001
            log.exception("live_review.deterministic.failed",
                          extra={"strategy_id": sid, "error": str(exc)})
    return out


def format_live_review_block(db_path: str) -> str:
    """Text block for agent briefings: live numbers + rule verdict per active.

    Advisory only — the agent decides. Young strategies (< 14 d) get the
    grace note so the stale/inactive rule is not applied prematurely."""
    lines = ["LIVE REVIEW (sim fills, rolling-20; rule verdict is ADVISORY — you decide):"]
    try:
        results = review_all_active_deterministic(db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        return f"LIVE REVIEW: unavailable ({exc})"
    if not results:
        lines.append("  (no active strategies)")
        return "\n".join(lines)
    # worst first
    def _key(t):
        pf = t[0].pf_20
        return (0 if t[1].action == "retire" else 1 if t[1].action == "retune" else 2,
                pf if (pf is not None and not math.isnan(pf)) else 9e9)
    for m, d, age in sorted(results, key=_key):
        pf = "n/a" if (m.pf_20 is None or math.isnan(m.pf_20)) else (
            "inf" if math.isinf(m.pf_20) else f"{m.pf_20:.2f}")
        er = "n/a" if (m.expectancy_20 is None or math.isnan(m.expectancy_20)) else (
            "inf" if math.isinf(m.expectancy_20) else f"{m.expectancy_20:+.2f}")
        verdict = d.action.upper()
        if age < 14 and d.action != "keep" and m.n_closed_trades < 10:
            verdict += f" (but only {age}d old, n={m.n_closed_trades} — grace period applies)"
        lines.append(
            f"  #{m.strategy_id} {m.strategy_name}: n={m.n_closed_trades} pf_20={pf} "
            f"E[R]={er} pnl=${m.total_realized_pnl:+.2f} consec_L={m.max_consec_losers} "
            f"stale={m.days_since_last_fire}d age={age}d → {verdict}: {d.reason}"
        )
    lines.append("  pnl/pf here are sim-fill P&L in dollars, gross of commission "
                 "(~$2-6 per round trip on micros) — shade marginal PFs down.")
    return "\n".join(lines)


def review_all_active_strategies(
    *, db_path: str
) -> list[tuple[StrategyMetrics, Decision]]:
    """Compute metrics + decision for every active strategy in the DB."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM active_strategies WHERE state = 'active' ORDER BY id"
        ).fetchall()
    results: list[tuple[StrategyMetrics, Decision]] = []
    for row in rows:
        sid = int(row["id"])
        try:
            metrics = compute_live_metrics(db_path=db_path, strategy_id=sid)
            decision = decide_action(metrics)
            results.append((metrics, decision))
        except Exception as exc:  # noqa: BLE001
            log.exception(
                "live_review.review.failed",
                extra={"strategy_id": sid, "error": str(exc)},
            )
    return results
