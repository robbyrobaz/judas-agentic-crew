"""Regime tagger.

Tags the current market regime using whatever data is available in the
``judas_crew.db`` SQLite. The output is a coarse label, not a model:

    {
      "vol_regime": "high" | "mid" | "low",
      "trend":      "trending" | "ranging" | "mixed",
      "leaders":    [<symbol>, ...]   # top 3 by |realized P&L| over lookback
    }

Heuristic — the schema currently has no bar table, so we fall back to the
``trades`` table's realized P&L:

* vol_regime: percentile of the recent N-day daily-pnl-stdev relative to the
  trailing 90-day window. >75th = high, <25th = low, else mid.
* trend: per-day net direction (sum of pnl) tallied — if >60% of days lean
  the same direction call it ``trending``, if <40% lean either way call it
  ``ranging``, else ``mixed``.
* leaders: top 3 symbols by absolute realized P&L over the lookback window.

When there is not enough data, the function returns conservative defaults
(``"mid"`` / ``"mixed"`` / ``[]``) rather than crashing.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.db.models import get_conn

log = logging.getLogger(__name__)


def _parse_iso_utc(ts: str) -> datetime | None:
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


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def _percentile_rank(value: float, sample: list[float]) -> float:
    """Return percentile rank of ``value`` within ``sample`` (0..1)."""
    if not sample:
        return 0.5
    below = sum(1 for s in sample if s < value)
    return below / len(sample)


def tag_regime(*, db_path: str, lookback_days: int = 30) -> dict:
    """Compute a coarse regime tag from recent realized P&L."""
    now = datetime.now(timezone.utc)
    lookback_start = now - timedelta(days=lookback_days)
    history_start = now - timedelta(days=90)

    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT symbol, pnl_dollars, opened_at, closed_at
            FROM trades
            WHERE status = 'closed'
              AND pnl_dollars IS NOT NULL
              AND datetime(COALESCE(closed_at, opened_at)) >= datetime(?)
            """,
            (history_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchall()

    # Group P&L by date (UTC) and by symbol.
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_pnl_lookback: dict[str, float] = defaultdict(float)
    symbol_abs_pnl: dict[str, float] = defaultdict(float)

    for row in rows:
        ts = _parse_iso_utc(str(row["closed_at"] or row["opened_at"] or ""))
        if ts is None:
            continue
        date_key = ts.date().isoformat()
        pnl = float(row["pnl_dollars"])
        daily_pnl[date_key] += pnl
        if ts >= lookback_start:
            daily_pnl_lookback[date_key] += pnl
            symbol_abs_pnl[str(row["symbol"])] += abs(pnl)

    # vol_regime: rolling 5-day stdev of daily pnl, percentile vs 90-day distribution.
    vol_regime = _vol_regime(daily_pnl, daily_pnl_lookback)

    # trend: % of days net-up vs net-down within lookback.
    n_days = len(daily_pnl_lookback)
    if n_days == 0:
        trend = "mixed"
    else:
        ups = sum(1 for v in daily_pnl_lookback.values() if v > 0)
        downs = sum(1 for v in daily_pnl_lookback.values() if v < 0)
        up_share = ups / n_days
        down_share = downs / n_days
        if up_share >= 0.6 or down_share >= 0.6:
            trend = "trending"
        elif up_share < 0.4 and down_share < 0.4:
            trend = "ranging"
        else:
            trend = "mixed"

    leaders = sorted(symbol_abs_pnl.items(), key=lambda kv: kv[1], reverse=True)
    leader_syms = [sym for sym, _ in leaders[:3]]

    return {"vol_regime": vol_regime, "trend": trend, "leaders": leader_syms}


def _vol_regime(
    daily_pnl_90d: dict[str, float],
    daily_pnl_lookback: dict[str, float],
) -> str:
    """High/mid/low based on lookback stdev's percentile within 90-day window."""
    if len(daily_pnl_90d) < 5 or len(daily_pnl_lookback) < 2:
        return "mid"

    # Rolling 5-day stdev across 90-day window.
    sorted_dates = sorted(daily_pnl_90d.keys())
    series = [daily_pnl_90d[d] for d in sorted_dates]
    rolling_stdevs: list[float] = []
    window = 5
    if len(series) < window:
        return "mid"
    for i in range(window, len(series) + 1):
        rolling_stdevs.append(_stdev(series[i - window : i]))

    current_stdev = _stdev(list(daily_pnl_lookback.values()))
    pct = _percentile_rank(current_stdev, rolling_stdevs)
    if pct >= 0.75:
        return "high"
    if pct <= 0.25:
        return "low"
    return "mid"
