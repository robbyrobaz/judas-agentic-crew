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
        candidate = conn.execute(
            "SELECT * FROM strategy_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            raise ValueError(f"Candidate {candidate_id} not found")

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

        if current:
            conn.execute(
                """
                UPDATE active_strategies
                SET state = 'retired', deactivated_at_utc = ?, notes = COALESCE(notes, '') || ?
                WHERE id = ?
                """,
                (_utc_now(), "\nSuperseded by automated promotion.", int(current["id"])),
            )

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
        conn.execute(
            "UPDATE strategy_candidates SET status = 'promoted' WHERE id = ?",
            (candidate_id,),
        )
        new_row = conn.execute(
            "SELECT * FROM active_strategies WHERE id = ?",
            (int(cur.lastrowid),),
        ).fetchone()
    return _row_to_active(new_row)


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
