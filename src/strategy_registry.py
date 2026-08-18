"""DB-backed strategy registry and promotion helpers."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.config import load_config
from src.db.models import get_conn, init_db

log = logging.getLogger(__name__)


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


def insert_active_strategy(
    *, symbol: str, strategy_family: str, params: dict[str, Any] | None = None,
    notes: str | None = None,
) -> ActiveStrategy:
    """Create a brand-new active_strategies row from scratch.

    Multiple actives per (symbol, family) are allowed — the next-version
    number is computed from the current max for that pair, but no
    previous row is retired. Use retire_strategy() deliberately when you
    actually want to retire one.
    
    If ``params`` is None, falls back to ``default_judas_params(symbol)``.
    """
    sym = symbol.upper()
    fam = str(strategy_family)
    use_params = params if params is not None else default_judas_params(sym)
    # Ensure symbol/strategy_family inside params match the row.
    use_params = {**use_params, "symbol": sym, "strategy_family": fam}
    with get_conn(_ensure_db()) as conn:
        _validate_lucid_ban(sym)  # refuse banned symbols at insert time too
        _validate_custom_link(conn, use_params)
        # BEGIN IMMEDIATE so the MAX(version) read and the INSERT are atomic —
        # otherwise two concurrent inserts read the same max and create duplicate
        # active rows (db version race).
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT MAX(version) AS v FROM active_strategies "
            "WHERE symbol = ? AND strategy_family = ?",
            (sym, fam),
        ).fetchone()
        next_version = (int(prior["v"]) + 1) if (prior and prior["v"] is not None) else 1
        cur = conn.execute(
            """
            INSERT INTO active_strategies
                (symbol, strategy_family, version, params_json, metrics_json,
                 state, activated_at_utc, notes)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (sym, fam, next_version, json.dumps(use_params),
             json.dumps({"inserted_directly": True}),
             _utc_now(), notes or "Inserted by registrar."),
        )
        new_id = int(cur.lastrowid)
        # Supersede the prior active version of this (symbol, family, slot_key)
        # so two versions of the same setup can't both be active and double-fire
        # (cost real money 2026-06-17). For the `custom` family, the slot_key is
        # the custom_strategy_id (different csids load different code/architectures,
        # so they coexist). For non-custom families, slot_key == strategy_family
        # (param tweaks of the same setup supersede prior). Cross-symbol and
        # cross-family diversity per symbol is always preserved.
        new_slot_key = _slot_key_for(use_params)
        slot_filter, slot_params = _supersede_where_clause(
            sym, fam, new_slot_key, new_id
        )
        superseded = conn.execute(
            f"""
            UPDATE active_strategies
            SET state = 'superseded',
                deactivated_at_utc = ?,
                notes = COALESCE(notes, '') || ' [superseded by v' || ? || ']'
            WHERE {slot_filter}
            """,
            (_utc_now(), next_version, *slot_params),
        ).rowcount
        if superseded:
            log.info(
                "registry.superseded_prior_active symbol=%s family=%s slot_key=%s n=%d new_version=%d",
                sym, fam, new_slot_key, superseded, next_version,
            )
        row = conn.execute(
            "SELECT * FROM active_strategies WHERE id = ?", (new_id,),
        ).fetchone()
    return _row_to_active(row)


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

            symbol = str(candidate["symbol"])
            family = str(candidate["strategy_family"])

            # Inject family/name into params if missing, then validate.
            _pre_params = json.loads(candidate["params_json"] or "{}")
            if not isinstance(_pre_params, dict):
                # Valid JSON but not an object (e.g. a list) → reject cleanly
                # instead of crashing with AttributeError on .setdefault below.
                raise ValueError(
                    f"params_json must decode to a dict, got {type(_pre_params).__name__}"
                )
            _pre_params.setdefault("strategy_family", family)
            _pre_params.setdefault("strategy_name", f"{family}_{symbol.lower()}_auto")
            _pre_params.setdefault("execution_engine", "judas_native")
            _fixed_params = json.dumps(_pre_params)
            _validate_params_json(_fixed_params)
            _validate_lucid_ban(symbol)  # gate BEFORE custom_link so banned symbols fail first
            _validate_custom_link(conn, _pre_params)
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

            # Insert the new active version. Cross-FAMILY diversity per symbol is
            # intentional, but stacking multiple VERSIONS of the SAME (symbol,
            # family) is not diversity — they fire the same setup on the same bar
            # (1 trade becomes 2-3x size/loss; observed live 2026-06-17). Supersede
            # the prior active version of this (symbol, family) ATOMICALLY below so
            # there's no window where two versions double-fire.
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
                    _fixed_params,
                    candidate["metrics_json"],
                    candidate_id,
                    _utc_now(),
                    notes or "Automated paper promotion.",
                ),
            )
            new_id = int(cur.lastrowid)

            # See insert_active_strategy for the slot_key rationale. Same
            # csid-aware semantics: `custom` family uses custom_strategy_id,
            # other families use strategy_family as the slot key.
            new_slot_key = _slot_key_for(_pre_params)
            slot_filter, slot_params = _supersede_where_clause(
                symbol, family, new_slot_key, new_id
            )
            superseded = conn.execute(
                f"""
                UPDATE active_strategies
                SET state = 'superseded',
                    deactivated_at_utc = ?,
                    notes = COALESCE(notes, '') || ' [superseded by v' || ? || ']'
                WHERE {slot_filter}
                """,
                (_utc_now(), next_version, *slot_params),
            ).rowcount
            if superseded:
                log.info(
                    "registry.superseded_prior_active symbol=%s family=%s slot_key=%s n=%d new_version=%d",
                    symbol, family, new_slot_key, superseded, next_version,
                )
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

# Runtime keys that distinguish a real, runnable strategy from a hollow stub.
# Used by the zombie-retire guard to verify an LLM's "empty params" claim.
_JUDAS_NATIVE_RUNTIME_KEYS = (
    "detector_lookback_bars", "confirmation_bars", "pivot_length",
    "target_r", "stop_buffer_ticks", "min_sweep_ticks",
    "min_displacement_strength", "min_displacement_body_ratio", "max_sweep_age_bars",
)
_BUFFET_ZOO_TYPES = ("rsi", "ma_cross", "bollinger")
# Substrings that mark a retirement reason as a "params are empty / broken /
# wrong-for-engine" factual claim. When matched, the claim is VERIFIED against
# the real params before the retire is allowed — an LLM reviewer has twice
# fabricated these (2026-05-30: "empty params" and "carries bollinger params"
# on strategies that had neither). Genuine breakage still retires because the
# guard only refuses when _params_look_zombie() says the params ARE valid.
_ZOMBIE_REASON_MARKERS = (
    "zombie", "empty params", "params_json is empty", "params are empty",
    "no runtime", "lack any runtime", "lack runtime", "no judas keys",
    "missing detector", "no valid", "params lack", "hollow", "no params",
    "broken param", "wrong param", "invalid param", "params are broken",
    "param mismatch", "params mismatch", "engine mismatch", "wrong engine",
    "mismatched param", "engine/params",
)


def _params_look_zombie(params: dict[str, Any]) -> bool:
    """True only when params GENUINELY lack the runtime config to run.

    A judas_native strategy needs several detector keys; a buffet_zoo strategy
    needs a valid strategy_type. This is the code-of-record that a retirement
    reason claiming "empty/missing params" must agree with — if the params are
    actually complete, the claim is false and the retirement is refused.
    """
    engine = str(params.get("execution_engine", "")).lower()
    if engine == "buffet_zoo":
        return str(params.get("strategy_type", "")) not in _BUFFET_ZOO_TYPES
    # judas_native (and anything judas-like / unknown defaults to judas rules)
    present = [k for k in _JUDAS_NATIVE_RUNTIME_KEYS if k in params]
    return len(present) < 3


def _slot_key_for(params: dict[str, Any]) -> str:
    """The slot key for supersede semantics.

    For the `custom` family, the slot key is the custom_strategy_id —
    different csids load different code (different architectures/setup), so
    they coexist as separate actives on the same symbol. Without this,
    promoting a new iFVG variant on MBT would supersede an existing ATR-disp
    variant on the same symbol (loss of architectural diversity).

    For all other families (judas_native, buffet_zoo, etc.), the slot key is
    the strategy_family itself: param tweaks of the same setup type
    supersede prior versions (per the 2026-06-17 double-fire dedup rationale
    in commit 299aae5).

    Returns "" when the params lack the slot identifier (shouldn't happen
    for custom rows because _validate_custom_link runs first, but we handle
    it gracefully by falling back to family semantics).
    """
    engine = str(params.get("execution_engine", "")).lower()
    if engine == "custom":
        try:
            csid = int(params.get("custom_strategy_id") or 0)
        except (TypeError, ValueError):
            csid = 0
        if csid > 0:
            return f"csid:{csid}"
    # Non-custom: the family itself is the slot key (param tweaks supersede).
    return f"family:{params.get('strategy_family', '')}"


def _supersede_where_clause(
    symbol: str, family: str, slot_key: str, new_id: int
) -> tuple[str, tuple]:
    """Build the WHERE clause + params for a slot-aware supersede UPDATE.

    For csid:* slot keys, the clause matches only rows in the same (symbol,
    family) with the same custom_strategy_id — different csids coexist.
    For family:* slot keys, the clause matches all active rows in the
    (symbol, family) — param tweaks of the same setup supersede prior.
    """
    if slot_key.startswith("csid:"):
        csid = slot_key.split(":", 1)[1]
        # CAST both sides to INTEGER — params_json may store
        # custom_strategy_id as a JSON string ("232") in older rows even
        # though the slot key is the int form. JSON_EXTRACT preserves the
        # JSON type, so a plain equality compare would silently miss those
        # rows (5 affected in 2026-07-28 audit: 4385/4434/4435/4472/4484).
        return (
            "symbol = ? AND strategy_family = ? AND state = 'active' "
            "AND id != ? AND CAST(JSON_EXTRACT(params_json, '$.custom_strategy_id') AS INTEGER) = ?",
            (symbol, family, new_id, int(csid)),
        )
    # family:* — same as before (param tweaks supersede).
    return (
        "symbol = ? AND strategy_family = ? AND state = 'active' AND id != ?",
        (symbol, family, new_id),
    )


def _validate_lucid_ban(symbol: str) -> None:
    """Refuse to promote an active on a Lucid-banned symbol. The guard layer
    (src/research/lucid_guard.py:is_banned) is the source of truth: MET, MBT,
    DX are banned on this eval, period — broker execution layer being able to
    route an order is irrelevant, the portfolio refuses to fire. Without this
    gate, candidates on banned symbols used to slip through and sit as
    permanent zombies (demotions 4413/4404-4411 for DX/6J/ZF; demotions
    4426-4429 for MET/MBT promotion of 2026-08-18T13Z). Raises ValueError
    so the rejection is loud and the registrar can route the candidate to a
    non-banned symbol."""
    from src.research import lucid_guard as _lg
    if _lg.is_banned(symbol):
        banned = sorted(_lg.contract_cap.__globals__.get("RULES", {}).get("banned_symbols", set()))
        # Fallback string if we cannot reach RULES (defensive)
        ban_list = ",".join(banned) if banned else "see lucid_guard.banned_symbols"
        raise ValueError(
            f"symbol {symbol!r} is in lucid_guard banned_symbols ({ban_list}); "
            f"any active promoted on it is a permanent zombie. Drop the candidate "
            f"or route to a non-banned symbol."
        )


def _validate_custom_link(conn, params: dict[str, Any]) -> None:
    """Refuse to activate an execution_engine='custom' row without a LOADABLE
    code link. Root cause of the 2026-06 idle-strategies bug: custom rows were
    promoted with no custom_strategy_id, so the scan's custom branch bailed
    (custom_id<=0 → no signal, ever) and they sat silently dead for a week.
    Raises ValueError so the promotion fails loudly instead of birthing a
    zombie — the registrar can look up the right code id and retry."""
    if str(params.get("execution_engine", "")).lower() != "custom":
        return
    try:
        csid = int(params.get("custom_strategy_id") or 0)
    except (TypeError, ValueError):
        csid = 0
    if csid <= 0:
        raise ValueError(
            "custom engine requires a custom_strategy_id linking code in "
            "custom_strategies — without it the strategy can never fire. "
            "Find it: SELECT id,name FROM custom_strategies WHERE name LIKE '%...%'"
        )
    row = conn.execute(
        "SELECT id FROM custom_strategies WHERE id = ? AND active = 1 "
        "AND retired_at_utc IS NULL",
        (csid,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"custom_strategy_id={csid} does not match an active row in "
            f"custom_strategies — the code link is dead; strategy could never fire."
        )


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
            # Hallucinated-zombie guard: if the reason claims the params are
            # empty/missing runtime keys, VERIFY in code. An LLM reviewer must
            # not retire a fully-parameterized strategy on a fabricated factual
            # basis (this wiped 41 valid strategies on 2026-05-30). If the
            # params are actually complete, refuse the retirement.
            if any(mk in (reason or "").lower() for mk in _ZOMBIE_REASON_MARKERS):
                try:
                    _params = json.loads(row["params_json"] or "{}")
                except (TypeError, ValueError):
                    _params = {}
                if not _params_look_zombie(_params):
                    raise ValueError(
                        f"retire REFUSED for strategy {strategy_id}: reason claims "
                        f"empty/broken/mismatched params but params are VALID for "
                        f"engine={_params.get('execution_engine')!r} "
                        f"(keys={sorted(_params.keys())}). Reason was: {reason!r}"
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


def reject_candidate(candidate_id: int, reason: str) -> dict:
    """Mark a strategy_candidates row as rejected without promoting it.

    Returns {"ok": True, "candidate_id": int} or raises ValueError.
    """
    with get_conn(_ensure_db()) as conn:
        row = conn.execute(
            "SELECT id, status FROM strategy_candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Candidate {candidate_id} not found")
        conn.execute(
            "UPDATE strategy_candidates SET status = 'rejected', rationale = ? WHERE id = ?",
            (reason, candidate_id),
        )
        conn.commit()
    return {"ok": True, "candidate_id": candidate_id}


def reactivate_demoted(*, demotion_id: int) -> int:
    """Re-insert active_strategies row from preserved auto_demotions snapshot.

    Returns the new active_strategies.id. Raises ValueError if not found, already reactivated,
    or if the original demotion was a duplicate-fire structural retirement (these reactivations
    restore correlated exposure to a kept sibling and should be done via a fresh candidate with
    differentiated params, not via the preserved snapshot).
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
            # Guard: refuse to reactivate a duplicate-fire structural retirement.
            # Auto-demotions whose reason documents "Duplicate" identify an architecture that
            # was a strict subset of an existing kept sibling. Restoring it via reactivate_demoted
            # produces the same co-fire pattern within seconds (finding 9bcdd09e). Differentiated
            # revival must come through propose_candidate() with explicit regime-shifting params.
            reason_text = str(row["reason"] or "").lower()
            if "duplicate" in reason_text:
                raise ValueError(
                    f"auto_demotion {demotion_id} was a duplicate-fire structural retirement "
                    f"(reason contains 'duplicate'). Use propose_candidate() with differentiated "
                    f"params to revive, not reactivate_demoted(). Original reason: "
                    f"{str(row['reason'])[:200]}"
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
