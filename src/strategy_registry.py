"""DB-backed strategy registry and promotion helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.config import load_config
from src.db.models import get_conn, init_db


@dataclass
class ActiveStrategy:
    id: int
    symbol: str
    strategy_family: str
    version: int
    params: dict[str, Any]
    metrics: dict[str, Any]
    source_candidate_id: int | None
    state: str
    activated_at_utc: str
    deactivated_at_utc: str | None
    notes: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_path() -> str:
    import os
    from pathlib import Path

    return os.environ.get("JUDAS_DB_PATH", str(Path(__file__).parent.parent / "judas_crew.db"))


def _ensure_db() -> str:
    path = _db_path()
    init_db(path)
    return path


def default_judas_params(symbol: str) -> dict[str, Any]:
    cfg = load_config()
    return {
        "symbol": symbol.upper(),
        "strategy_name": "judas_base_1h",
        "strategy_family": "judas_1h",
        "execution_engine": "judas_native",
        "timeframe": "1H",
        "confirmation_bars": 4,
        "pivot_length": 2,
        "target_r": 2.0,
        "stop_buffer_ticks": 2,
        "min_sweep_ticks": 3,
        "min_displacement_strength": float(cfg.risk.min_displacement_strength),
        "min_displacement_body_ratio": float(cfg.risk.min_displacement_body_ratio),
        "max_sweep_age_bars": int(cfg.risk.max_sweep_age_bars),
        "min_atr_ratio": float(cfg.risk.atr_contraction_min_ratio),
        "require_fvg": False,
        "session_filter": "ny_open",
        "quality_score_min": int(cfg.crew.min_quality_score),
        "max_trades_per_day": int(cfg.risk.max_trades_per_day),
        "max_open_positions": int(cfg.risk.max_open_positions),
        "daily_loss_limit_dollars": float(cfg.risk.daily_loss_limit_dollars),
    }


def ensure_default_active_strategy(symbol: str, strategy_family: str = "judas_1h") -> ActiveStrategy:
    active = get_active_strategy(symbol=symbol, strategy_family=strategy_family)
    if active:
        return active
    params = default_judas_params(symbol)
    with get_conn(_ensure_db()) as conn:
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json, state, activated_at_utc, notes)
            VALUES (?, ?, 1, ?, ?, 'active', ?, ?)
            """,
            (
                symbol.upper(),
                strategy_family,
                json.dumps(params),
                json.dumps({"seeded": True}),
                _utc_now(),
                "Seeded default paper strategy.",
            ),
        )
        strategy_id = int(cur.lastrowid)
        row = conn.execute("SELECT * FROM active_strategies WHERE id = ?", (strategy_id,)).fetchone()
    return _row_to_active(row)


def _row_to_active(row) -> ActiveStrategy:
    return ActiveStrategy(
        id=int(row["id"]),
        symbol=str(row["symbol"]),
        strategy_family=str(row["strategy_family"]),
        version=int(row["version"]),
        params=json.loads(row["params_json"] or "{}"),
        metrics=json.loads(row["metrics_json"] or "{}"),
        source_candidate_id=int(row["source_candidate_id"]) if row["source_candidate_id"] is not None else None,
        state=str(row["state"]),
        activated_at_utc=str(row["activated_at_utc"]),
        deactivated_at_utc=str(row["deactivated_at_utc"]) if row["deactivated_at_utc"] else None,
        notes=str(row["notes"]) if row["notes"] else None,
    )


def get_active_strategy(symbol: str, strategy_family: str = "judas_1h") -> ActiveStrategy | None:
    with get_conn(_ensure_db()) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM active_strategies
            WHERE symbol = ? AND strategy_family = ? AND state = 'active'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (symbol.upper(), strategy_family),
        ).fetchone()
    return _row_to_active(row) if row else None


def get_active_or_default(symbol: str, strategy_family: str = "judas_1h") -> ActiveStrategy:
    return get_active_strategy(symbol=symbol, strategy_family=strategy_family) or ensure_default_active_strategy(
        symbol=symbol,
        strategy_family=strategy_family,
    )


def create_candidate(
    *,
    symbol: str,
    strategy_family: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    decision: str,
    rationale: str,
    status: str = "candidate",
    source_experiment_id: int | None = None,
) -> int:
    with get_conn(_ensure_db()) as conn:
        cur = conn.execute(
            """
            INSERT INTO strategy_candidates
                (ts_utc, symbol, strategy_family, source_experiment_id, params_json, metrics_json,
                 decision, rationale, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                symbol.upper(),
                strategy_family,
                source_experiment_id,
                json.dumps(params),
                json.dumps(metrics),
                decision,
                rationale,
                status,
            ),
        )
        return int(cur.lastrowid)


def promote_candidate(candidate_id: int, notes: str | None = None) -> ActiveStrategy:
    with get_conn(_ensure_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            candidate = conn.execute(
                "SELECT * FROM strategy_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if not candidate:
                raise ValueError(f"Candidate {candidate_id} not found")

            # Validate params_json before doing anything destructive.
            _validate_params_json(candidate["params_json"])

            symbol = str(candidate["symbol"])
            family = str(candidate["strategy_family"])
            current = conn.execute(
                """
                SELECT *
                FROM active_strategies
                WHERE symbol = ? AND strategy_family = ? AND state = 'active'
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (symbol, family),
            ).fetchone()
            next_version = (int(current["version"]) + 1) if current else 1

            # Insert new active row WITHOUT retiring any prior actives for
            # this (symbol, family). Multiple strategies per symbol are
            # explicitly allowed per the Operator's goals preamble —
            # diversity reduces blow-up risk and increases dollar capture
            # across regimes. Retiring should be a deliberate decision
            # (negative P&L on a real sample, broken regime fit, etc.)
            # via retire_strategy(), not an automatic side effect of
            # promoting another candidate.
            cur = conn.execute(
                """
                INSERT INTO active_strategies
                    (symbol, strategy_family, version, params_json, metrics_json, source_candidate_id,
                     state, activated_at_utc, notes)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    symbol,
                    family,
                    next_version,
                    candidate["params_json"],
                    candidate["metrics_json"],
                    candidate_id,
                    _utc_now(),
                    notes or "Automated paper promotion.",
                ),
            )
            new_id = int(cur.lastrowid)

            conn.execute(
                "UPDATE strategy_candidates SET status = 'promoted' WHERE id = ?",
                (candidate_id,),
            )
            new_row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ?",
                (new_id,),
            ).fetchone()
            conn.commit()
            return _row_to_active(new_row)
        except Exception:
            conn.rollback()
            raise


_REQUIRED_PARAM_KEYS = ("symbol",)
_REQUIRED_PARAM_NAME_ALTERNATES = ("strategy_name", "strategy_family")


def _validate_params_json(raw: str | None) -> dict[str, Any]:
    """Parse + validate a candidate's params_json blob. Raise ValueError if bad."""
    if raw is None or raw == "":
        raise ValueError("params_json is empty")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"params_json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"params_json must decode to a dict, got {type(parsed).__name__}")
    for key in _REQUIRED_PARAM_KEYS:
        if key not in parsed:
            raise ValueError(f"params_json missing required key: {key}")
    if not any(alt in parsed for alt in _REQUIRED_PARAM_NAME_ALTERNATES):
        raise ValueError(
            "params_json must include one of: " + ", ".join(_REQUIRED_PARAM_NAME_ALTERNATES)
        )
    return parsed


def retire_strategy(
    *,
    strategy_id: int,
    reason: str,
    metrics_snapshot: dict[str, Any],
) -> int:
    """Atomically retire an active_strategies row and insert an auto_demotions row.

    Returns the auto_demotions.id created. Raises ValueError if not found or already retired.
    """
    with get_conn(_ensure_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM active_strategies WHERE id = ? AND state = 'active'",
                (int(strategy_id),),
            ).fetchone()
            if not row:
                raise ValueError(
                    f"active strategy {strategy_id} not found or not active"
                )
            now = _utc_now()
            conn.execute(
                """
                UPDATE active_strategies
                SET state = 'retired',
                    deactivated_at_utc = ?,
                    notes = COALESCE(notes, '') || ?
                WHERE id = ?
                """,
                (now, f"\nAuto-retired: {reason}", int(strategy_id)),
            )
            cur = conn.execute(
                """
                INSERT INTO auto_demotions
                    (ts_utc, strategy_id, symbol, strategy_family, version,
                     params_json, metrics_snapshot_json, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    int(strategy_id),
                    str(row["symbol"]),
                    str(row["strategy_family"]),
                    int(row["version"]),
                    str(row["params_json"] or "{}"),
                    json.dumps(metrics_snapshot or {}),
                    reason,
                ),
            )
            demotion_id = int(cur.lastrowid)
            conn.commit()
            return demotion_id
        except Exception:
            conn.rollback()
            raise


def reactivate_demoted(*, demotion_id: int) -> int:
    """Re-insert active_strategies row from preserved auto_demotions snapshot.

    Returns the new active_strategies.id. Raises ValueError if not found or already reactivated.
    """
    with get_conn(_ensure_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM auto_demotions WHERE id = ?",
                (int(demotion_id),),
            ).fetchone()
            if not row:
                raise ValueError(f"auto_demotion {demotion_id} not found")
            if row["reactivated_at_utc"]:
                raise ValueError(
                    f"auto_demotion {demotion_id} already reactivated at {row['reactivated_at_utc']}"
                )
            now = _utc_now()
            new_version = int(row["version"]) + 1
            cur = conn.execute(
                """
                INSERT INTO active_strategies
                    (symbol, strategy_family, version, params_json, metrics_json,
                     state, activated_at_utc, notes)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    str(row["symbol"]),
                    str(row["strategy_family"]),
                    new_version,
                    str(row["params_json"]),
                    str(row["metrics_snapshot_json"]),
                    now,
                    f"Reactivated from demotion #{int(row['id'])}",
                ),
            )
            new_id = int(cur.lastrowid)
            conn.execute(
                """
                UPDATE auto_demotions
                SET reactivated_at_utc = ?, reactivated_strategy_id = ?
                WHERE id = ?
                """,
                (now, new_id, int(demotion_id)),
            )
            conn.commit()
            return new_id
        except Exception:
            conn.rollback()
            raise


def list_active_strategies() -> list[dict[str, Any]]:
    with get_conn(_ensure_db()) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM active_strategies
            WHERE state = 'active'
            ORDER BY symbol, strategy_family, version DESC
            """
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "symbol": str(row["symbol"]),
            "strategy_family": str(row["strategy_family"]),
            "version": int(row["version"]),
            "params": json.loads(row["params_json"] or "{}"),
            "metrics": json.loads(row["metrics_json"] or "{}"),
            "source_candidate_id": int(row["source_candidate_id"]) if row["source_candidate_id"] is not None else None,
            "state": str(row["state"]),
            "activated_at_utc": str(row["activated_at_utc"]),
            "notes": str(row["notes"]) if row["notes"] else None,
        }
        for row in rows
    ]


def activate_seed_strategy(
    *,
    symbol: str,
    strategy_family: str,
    params: dict[str, Any],
    metrics: dict[str, Any],
    notes: str,
) -> int:
    with get_conn(_ensure_db()) as conn:
        existing = conn.execute(
            """
            SELECT id
            FROM active_strategies
            WHERE symbol = ? AND strategy_family = ? AND state = 'active'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (symbol.upper(), strategy_family),
        ).fetchone()
        if existing:
            return int(existing["id"])
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json, state, activated_at_utc, notes)
            VALUES (?, ?, 1, ?, ?, 'active', ?, ?)
            """,
            (
                symbol.upper(),
                strategy_family,
                json.dumps(params),
                json.dumps(metrics),
                _utc_now(),
                notes,
            ),
        )
        return int(cur.lastrowid)
