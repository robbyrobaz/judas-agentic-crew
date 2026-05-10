"""Daily brief composition + persistence (Phase 4).

Pulls the prior trading-day's fires/fills/P&L from the ``trades`` table,
computes a structured ``summary_json`` payload that conforms to the
contract shared with the dashboard worker, and renders a Markdown body.

Contract for ``summary_json``:

    {
      "fires": int,
      "fills": int,
      "pnl_total": float,
      "pnl_by_strategy": [
          {"strategy_id": int, "name": str, "pnl": float, "trades": int},
          ...
      ],
      "regime": {"vol_regime": str, "trend": str, "leaders": [str, ...]},
      "surprises": [
          {"strategy_id": int, "name": str, "expected_pnl": float,
           "actual_pnl": float, "z": float},
          ...
      ],
      "recommended_actions": [
          {"type": "retire", "strategy_id": int, "reason": str,
           "demotion_id": int | None},
          {"type": "review", "strategy_id": int, "reason": str},
          ...
      ]
    }
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.db.models import get_conn, init_db
from src.research.live_review import compute_live_metrics
from src.research.regime import tag_regime

log = logging.getLogger(__name__)

NY_TZ = ZoneInfo("America/New_York")

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _briefs_dir() -> Path:
    """Resolve the directory where Markdown briefs are mirrored.

    Honours ``JUDAS_BRIEFS_DIR`` and otherwise defaults to
    ``<repo_root>/outputs/briefs``.
    """
    override = os.environ.get("JUDAS_BRIEFS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return _REPO_ROOT / "outputs" / "briefs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _yesterday_et() -> str:
    """Return yesterday's date in America/New_York as YYYY-MM-DD."""
    today_et = datetime.now(NY_TZ).date()
    return (today_et - timedelta(days=1)).isoformat()


def _et_day_to_utc_window(brief_date: str) -> tuple[str, str]:
    """Convert a YYYY-MM-DD ET date into [start_utc_iso, end_utc_iso) bounds."""
    d = date.fromisoformat(brief_date)
    start_et = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=NY_TZ)
    end_et = start_et + timedelta(days=1)
    start_utc = start_et.astimezone(timezone.utc)
    end_utc = end_et.astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start_utc.strftime(fmt), end_utc.strftime(fmt)


def _strategy_name_from_params(row: sqlite3.Row | dict | None) -> str:
    if row is None:
        return "unknown"
    try:
        family = row["strategy_family"]
        symbol = row["symbol"]
        version = row["version"]
        params_raw = row["params_json"]
    except (KeyError, IndexError, TypeError):
        return "unknown"
    try:
        params = json.loads(params_raw or "{}")
    except (TypeError, json.JSONDecodeError):
        params = {}
    name = params.get("strategy_name")
    if name:
        return str(name)
    return f"{symbol}/{family}/v{version}"


def _strategy_lookup(conn: sqlite3.Connection, strategy_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, symbol, strategy_family, version, params_json "
        "FROM active_strategies WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _backtest_expectancy(params_json: str | None) -> dict | None:
    """Pull backtest expectancy from a params payload if present.

    We look for two shapes in ``params_json``:
      {"backtest": {"expected_pnl": ..., "stdev_pnl": ...}}
      {"expected_pnl": ..., "stdev_pnl": ...}
    Anything else returns None — caller skips surprise computation.
    """
    if not params_json:
        return None
    try:
        params = json.loads(params_json)
    except (TypeError, json.JSONDecodeError):
        return None
    bt = params.get("backtest") if isinstance(params, dict) else None
    candidate = bt if isinstance(bt, dict) else params
    if not isinstance(candidate, dict):
        return None
    if "expected_pnl" in candidate and "stdev_pnl" in candidate:
        try:
            return {
                "expected_pnl": float(candidate["expected_pnl"]),
                "stdev_pnl": float(candidate["stdev_pnl"]),
            }
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------


def compose_daily_brief(
    *, db_path: str, brief_date: str | None = None
) -> dict:
    """Compose a daily brief for ``brief_date`` (ET, YYYY-MM-DD)."""
    if brief_date is None:
        brief_date = _yesterday_et()

    start_utc, end_utc = _et_day_to_utc_window(brief_date)

    init_db(db_path)

    with get_conn(db_path) as conn:
        # Fires: all signals (any direction) created during the ET day.
        fires_row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM signals
            WHERE datetime(ts_utc) >= datetime(?)
              AND datetime(ts_utc) <  datetime(?)
            """,
            (start_utc, end_utc),
        ).fetchone()
        fires = int(fires_row["n"] if fires_row else 0)

        # Fills + per-strategy P&L: trades opened during the ET day.
        trades = conn.execute(
            """
            SELECT id, strategy_id, symbol, pnl_dollars, status,
                   opened_at, closed_at
            FROM trades
            WHERE datetime(opened_at) >= datetime(?)
              AND datetime(opened_at) <  datetime(?)
            """,
            (start_utc, end_utc),
        ).fetchall()

        # A "fill" = any non-cancelled trade row opened on the ET day.
        fills = sum(1 for t in trades if t["status"] != "cancelled")

        pnl_by_sid: dict[int, dict[str, Any]] = {}
        pnl_total = 0.0
        for t in trades:
            sid = t["strategy_id"]
            if sid is None:
                continue
            pnl = float(t["pnl_dollars"] or 0.0)
            pnl_total += pnl
            entry = pnl_by_sid.setdefault(int(sid), {"pnl": 0.0, "trades": 0})
            entry["pnl"] += pnl
            entry["trades"] += 1

        pnl_by_strategy: list[dict[str, Any]] = []
        for sid, agg in pnl_by_sid.items():
            sname_row = _strategy_lookup(conn, sid)
            pnl_by_strategy.append(
                {
                    "strategy_id": int(sid),
                    "name": _strategy_name_from_params(sname_row),
                    "pnl": float(agg["pnl"]),
                    "trades": int(agg["trades"]),
                }
            )
        pnl_by_strategy.sort(key=lambda d: d["pnl"], reverse=True)

        # Surprises: trades whose realized P&L is > 2σ from backtest expectancy.
        surprises: list[dict[str, Any]] = []
        for sid, agg in pnl_by_sid.items():
            srow = _strategy_lookup(conn, sid)
            if srow is None:
                continue
            bt = _backtest_expectancy(srow.get("params_json"))
            if bt is None:
                log.debug(
                    "brief.surprise.skipped_no_backtest",
                    extra={"strategy_id": sid},
                )
                continue
            stdev = bt["stdev_pnl"]
            expected = bt["expected_pnl"]
            actual = float(agg["pnl"])
            if stdev <= 0 or math.isnan(stdev):
                continue
            z = (actual - expected) / stdev
            if abs(z) > 2.0:
                surprises.append(
                    {
                        "strategy_id": int(sid),
                        "name": _strategy_name_from_params(srow),
                        "expected_pnl": expected,
                        "actual_pnl": actual,
                        "z": float(z),
                    }
                )

        # Recommended actions:
        # 1) retire entries from auto_demotions in last 24h.
        # 2) review entries from active strategies whose live PF < 0.9.
        recommended: list[dict[str, Any]] = []
        cutoff_utc = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        demotion_rows = conn.execute(
            """
            SELECT id, strategy_id, reason
            FROM auto_demotions
            WHERE datetime(ts_utc) >= datetime(?)
            ORDER BY ts_utc DESC
            """,
            (cutoff_utc,),
        ).fetchall()
        for d in demotion_rows:
            recommended.append(
                {
                    "type": "retire",
                    "strategy_id": int(d["strategy_id"]),
                    "reason": str(d["reason"] or ""),
                    "demotion_id": int(d["id"]),
                }
            )

        # Review actions: scan active strategies (snapshot ids first).
        active_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM active_strategies WHERE state = 'active'"
            ).fetchall()
        ]
    # Compute review actions outside the connection — compute_live_metrics
    # opens its own connection.
    seen_in_retire = {r["strategy_id"] for r in recommended if r["type"] == "retire"}
    for sid in active_ids:
        if sid in seen_in_retire:
            continue
        try:
            m = compute_live_metrics(db_path=db_path, strategy_id=sid)
        except Exception:  # noqa: BLE001
            log.debug("brief.review.compute_failed", extra={"strategy_id": sid})
            continue
        pf = m.pf_20
        if pf is None or math.isnan(pf) or math.isinf(pf):
            continue
        if pf < 0.9:
            recommended.append(
                {
                    "type": "review",
                    "strategy_id": sid,
                    "reason": f"live pf_20={pf:.2f} < 0.9 over last week",
                }
            )

    regime = tag_regime(db_path=db_path)

    summary = {
        "fires": fires,
        "fills": int(fills),
        "pnl_total": float(pnl_total),
        "pnl_by_strategy": pnl_by_strategy,
        "regime": regime,
        "surprises": surprises,
        "recommended_actions": recommended,
    }

    content_md = _render_markdown(brief_date=brief_date, summary=summary)
    return {"summary": summary, "content_md": content_md}


def _render_markdown(*, brief_date: str, summary: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Daily Brief — {brief_date}")
    lines.append("")

    lines.append("## Performance")
    lines.append("")
    lines.append(f"- **Fires:** {summary['fires']}")
    lines.append(f"- **Fills:** {summary['fills']}")
    lines.append(f"- **Total P&L:** ${summary['pnl_total']:.2f}")
    if summary["pnl_by_strategy"]:
        lines.append("")
        lines.append("### By strategy")
        lines.append("")
        lines.append("| ID | Name | Trades | P&L |")
        lines.append("|---:|:---|---:|---:|")
        for entry in summary["pnl_by_strategy"]:
            lines.append(
                f"| {entry['strategy_id']} | {entry['name']} | "
                f"{entry['trades']} | ${entry['pnl']:.2f} |"
            )
    lines.append("")

    regime = summary.get("regime") or {}
    lines.append("## Regime")
    lines.append("")
    lines.append(f"- **Volatility:** {regime.get('vol_regime', 'mid')}")
    lines.append(f"- **Trend:** {regime.get('trend', 'mixed')}")
    leaders = regime.get("leaders") or []
    lines.append(
        "- **Leaders:** " + (", ".join(leaders) if leaders else "_none_")
    )
    lines.append("")

    lines.append("## Surprises")
    lines.append("")
    if summary["surprises"]:
        for s in summary["surprises"]:
            lines.append(
                f"- {s['name']} (id={s['strategy_id']}): "
                f"actual ${s['actual_pnl']:.2f} vs expected "
                f"${s['expected_pnl']:.2f} (z={s['z']:.2f})"
            )
    else:
        lines.append("_None — all strategies within 2σ of backtest expectancy._")
    lines.append("")

    lines.append("## Recommendations")
    lines.append("")
    if summary["recommended_actions"]:
        for a in summary["recommended_actions"]:
            if a["type"] == "retire":
                lines.append(
                    f"- **RETIRE** strategy {a['strategy_id']}: {a['reason']}"
                )
            elif a["type"] == "review":
                lines.append(
                    f"- **REVIEW** strategy {a['strategy_id']}: {a['reason']}"
                )
            else:
                lines.append(f"- {a['type']} strategy {a['strategy_id']}: {a.get('reason', '')}")
    else:
        lines.append("_No actions today._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------


def persist_daily_brief(
    *,
    db_path: str,
    brief_date: str,
    content_md: str,
    summary_json: dict,
) -> int:
    """INSERT OR REPLACE into ``daily_briefs`` keyed on ``brief_date``."""
    init_db(db_path)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(summary_json, default=str)

    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO daily_briefs (brief_date, created_at_utc, content_md, summary_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(brief_date) DO UPDATE SET
                created_at_utc = excluded.created_at_utc,
                content_md     = excluded.content_md,
                summary_json   = excluded.summary_json,
                acknowledged_at_utc = NULL
            """,
            (brief_date, created, content_md, payload),
        )
        row = conn.execute(
            "SELECT id FROM daily_briefs WHERE brief_date = ?",
            (brief_date,),
        ).fetchone()
        brief_id = int(row["id"]) if row else 0

    # Mirror the markdown to outputs/briefs/.
    briefs_dir = _briefs_dir()
    try:
        briefs_dir.mkdir(parents=True, exist_ok=True)
        out_path = briefs_dir / f"{brief_date}.md"
        out_path.write_text(content_md, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "brief.markdown_file_write_failed",
            extra={"brief_date": brief_date, "error": str(exc)},
        )

    return brief_id
