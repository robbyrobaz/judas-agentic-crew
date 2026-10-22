"""Task #2859 -- SUPERSEDURE_COLLATERAL_RESTORE_PLAN.

Read-only. Builds the full restore ledger for every active_strategies row that
went non-active on/after 2026-10-14, classifies each as

    RESTORE | SUPERSEDED-BY-BETTER | LEAVE DEAD

and emits the exact promotion instruction for each RESTORE row, including
whether the restore would itself supersede a live sibling.

Outputs (all in this directory):
    ledger.tsv        one row per deactivated strategy, every required column
    restore_only.tsv  the RESTORE rows sorted by BT dollars descending
    summary.json      counts + the headline dollar number
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = Path("judas_crew.db")
OUT = Path("_restore_ledger")
OUT.mkdir(exist_ok=True)
CUTOFF = "2026-10-14"
LEGAL = {"MGC", "MNQ", "MCL", "6J", "ZF"}
BANNED = {"DX", "MET", "MBT"}
FAST_TF = {"5m", "15m"}


def _f(d: dict, *keys):
    """First float-coercible key present in d."""
    for k in keys:
        if k in d and d[k] not in (None, "", "None"):
            try:
                return float(d[k])
            except (TypeError, ValueError):
                continue
    return None


def _loads(s):
    if not s:
        return {}
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _tf_of(params: dict) -> str:
    tf = str(params.get("timeframe") or "").strip().lower()
    if tf in ("", "none"):
        # family often encodes it: custom_5m / silver_bullet_..._15m
        fam = str(params.get("strategy_family") or "").lower()
        for t in ("5m", "15m", "1h"):
            if fam.endswith("_" + t) or f"_{t}_" in fam:
                return t
        if fam.endswith("_1h") or "_1h_" in fam:
            return "1h"
    return tf


def _csid_of(params: dict):
    v = params.get("custom_strategy_id")
    if v in (None, "", "None"):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def span_verdict(metrics: dict, tf: str) -> str:
    """Is the 'days=90' stamp REAL for this envelope?

    Per finding 9108: metrics_json.days is the REQUESTED window, not the
    realised one. New envelopes stamp bars_span_days/first_bar/last_bar
    (commit 8e6cf38); anything older does not.
    """
    real = metrics.get("bars_span_days")
    if real not in (None, "", "None"):
        try:
            d = int(float(real))
            return f"REAL_{d}d"
        except (TypeError, ValueError):
            pass
    if tf in FAST_TF:
        return "UNVERIFIED_5m15m_SINGLE_REQUEST_CAP"
    for k in ("backtest_window_days", "days"):
        if k in metrics:
            return f"LABELLED_{metrics[k]}d_UNVERIFIED"
    return "UNKNOWN"


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    dead = conn.execute(
        "SELECT * FROM active_strategies WHERE state <> 'active' "
        "AND deactivated_at_utc >= ? ORDER BY deactivated_at_utc, id",
        (CUTOFF,),
    ).fetchall()
    live = conn.execute(
        "SELECT * FROM active_strategies WHERE state = 'active'"
    ).fetchall()
    cands = {
        int(r["id"]): r
        for r in conn.execute("SELECT * FROM strategy_candidates").fetchall()
    }
    cs_live = {
        int(r["id"]): r
        for r in conn.execute(
            "SELECT id, name, symbol, active, retired_at_utc FROM custom_strategies"
        ).fetchall()
    }

    # ---- index the live book so we can answer "would this restore collide?"
    # Supersede WHERE (src/strategy_registry.py:_supersede_where_clause) for a
    # custom row is  symbol = ? AND strategy_family = ? AND state='active'
    # AND CAST(JSON_EXTRACT(params_json,'$.custom_strategy_id') AS INT) = ?
    #  -- i.e. (symbol, family, csid). Timeframe is NOT part of it.
    live_by_slot: dict[tuple, list[dict]] = defaultdict(list)
    live_by_sym_csid: dict[tuple, list[dict]] = defaultdict(list)
    for r in live:
        p = _loads(r["params_json"])
        m = _loads(r["metrics_json"])
        entry = {
            "id": int(r["id"]),
            "symbol": r["symbol"],
            "family": r["strategy_family"],
            "csid": _csid_of(p),
            "tf": _tf_of(p),
            "pf": _f(m, "profit_factor", "pf"),
            "n": _f(m, "n_trades", "n", "n_signals"),
            "usd": _f(m, "total_pnl"),
        }
        if entry["csid"]:
            live_by_slot[(entry["symbol"], entry["family"], entry["csid"])].append(entry)
            live_by_sym_csid[(entry["symbol"], entry["csid"])].append(entry)

    rows = []
    for r in dead:
        p = _loads(r["params_json"])
        m = _loads(r["metrics_json"])
        cid = r["source_candidate_id"]
        cand = cands.get(int(cid)) if cid else None
        cm = _loads(cand["metrics_json"]) if cand else {}
        # Prefer the active's own stamped envelope; fall back to the candidate.
        src = "active.metrics" if _f(m, "profit_factor", "pf") is not None else (
            "candidate.metrics" if _f(cm, "profit_factor", "pf") is not None else "NONE"
        )
        eff = m if src == "active.metrics" else (cm or m)

        pf = _f(eff, "profit_factor", "pf")
        n = _f(eff, "n_trades", "n", "n_signals")
        wr = _f(eff, "winrate", "wr")
        er = _f(eff, "expectancy_r", "avg_r")
        usd = _f(eff, "total_pnl")
        dd = _f(eff, "max_drawdown")
        cmodel = str(eff.get("cost_model") or m.get("cost_model") or cm.get("cost_model") or "")
        tf = _tf_of(p) or (""
            if "5m" in str(r["strategy_family"]) else
            "15m" if "15m" in str(r["strategy_family"]) else
            "1h" if "1h" in str(r["strategy_family"]) else "?")
        csid = _csid_of(p)

        symbol = r["symbol"]
        illegal = symbol in BANNED

        # BT-LURE: PF>3 AND WR>90% jointly is the documented lure signature.
        lure = bool(pf and wr and pf > 3.0 and wr > 0.90)

        # ---- collision analysis
        exact = live_by_slot.get((symbol, r["strategy_family"], csid), []) if csid else []
        wider = [e for e in live_by_sym_csid.get((symbol, csid), [])] if csid else []

        slot_free = not exact
        successor = ""
        if exact:
            best = max(exact, key=lambda e: (e["pf"] or 0, e["usd"] or 0))
            dominates = (
                (best["pf"] or 0) >= (pf or 0) and (best["usd"] or 0) >= (usd or 0)
            )
            successor = (
                f"#{best['id']} pf={best['pf']} usd={best['usd']} "
                f"dom={'YES' if dominates else 'NO'}"
            )

        # ---- verdict
        metric_ok = bool(cf_stamp := cmodel.startswith("v1")) and (n or 0) >= 20 and not lure and pf is not None and pf >= 1.3
        if illegal:
            verdict, why = "LEAVE-DEAD-BANNED-SYMBOL", f"{symbol} is banned on this venue"
        elif not cf_stamp:
            verdict, why = "LEAVE-DEAD-NO-COST-STAMP", f"cost_model={cmodel or 'MISSING'}"
        elif lure:
            verdict, why = "LEAVE-DEAD-BT-LURE", f"PF={pf:.2f} + WR={wr:.1%} on n={int(n or 0)}"
        elif (n or 0) < 20:
            verdict, why = "LEAVE-DEAD-LOW-N", f"n={int(n or 0 or 0)}"
        elif pf is None:
            verdict, why = "LEAVE-DEAD-NO-PF", "no profit_factor in either envelope"
        elif pf < 1.3:
            verdict, why = "LEAVE-DEAD-LOW-PF", f"PF={pf:.2f} n={int(n or 0)}"
        elif exact and successor.endswith("dom=YES"):
            verdict, why = "SUPERSEDED-BY-BETTER", successor
        elif slot_free:
            verdict, why = "RESTORE", "slot (symbol,family,csid) is free"
        else:
            verdict, why = "BLOCKED-SLOT-TAKEN-NOT-DOMINATED", successor

        # A restore is only UNSAFE if promoting it would evict a live row. With
        # the guard now wired (commit 441ee81) the promotion is REFUSED loudly
        # rather than silently -- so it is "blocked pending explicit retire",
        # not "silently destructive".
        slot_key = f"{symbol}_{tf}_{r['strategy_family']}_csid{csid}_v{r['version']}"
        cand_status = ""
        if cand is not None:
            cand_status = str(cand["status"])
        code_live = ""
        if csid:
            cr = cs_live.get(csid)
            code_live = "LOADABLE" if (cr and cr["active"] and not cr["retired_at_utc"]) else (
                "MISSING" if cr is None else "RETIRED"
            )

        rows.append({
            "id": int(r["id"]),
            "symbol": symbol,
            "strategy_family": r["strategy_family"],
            "version": int(r["version"]),
            "source_candidate_id": int(cid) if cid else None,
            "cand_status": cand_status,
            "deactivated_at_utc": r["deactivated_at_utc"],
            "state": r["state"],
            "tf": tf,
            "csid": csid,
            "code_link": code_live,
            "BT_PF": None if pf is None else round(pf, 3),
            "BT_n": None if n is None else int(n),
            "BT_WR": None if wr is None else round(wr, 3),
            "BT_ER": None if er is None else round(er, 3),
            "BT_$": None if usd is None else round(usd, 2),
            "BT_maxDD": None if dd is None else round(dd, 2),
            "cost_model": cmodel or "MISSING",
            "metric_src": src,
            "span_is_90d_real": span_verdict(eff, tf),
            "is_lure": lure,
            "slot_key": slot_key,
            "slot_free": slot_free,
            "restore_would_evict": ";".join(f"#{e['id']}" for e in exact),
            "same_csid_other_tf_live": ";".join(
                f"#{e['id']}@{e['tf']}" for e in wider if e not in exact
            ),
            "verdict": verdict,
            "why": why,
        })

    cols = list(rows[0].keys())
    (OUT / "ledger.tsv").write_text(
        "\t".join(cols) + "\n"
        + "\n".join("\t".join("" if r[c] is None else str(r[c]) for c in cols) for r in rows)
        + "\n"
    )

    restores = sorted(
        [r for r in rows if r["verdict"] == "RESTORE"],
        key=lambda r: -(r["BT_$"] or 0),
    )
    (OUT / "restore_only.tsv").write_text(
        "\t".join(cols) + "\n"
        + "\n".join("\t".join("" if r[c] is None else str(r[c]) for c in cols) for r in restores)
        + "\n"
    )

    vcount: dict[str, int] = defaultdict(int)
    for r in rows:
        vcount[r["verdict"]] += 1

    rest_dollars = sum(r["BT_$"] or 0 for r in restores)
    all_stamped_dollars = sum(
        (r["BT_$"] or 0)
        for r in rows
        if r["cost_model"].startswith("v1") and (r["BT_n"] or 0) >= 20
    )

    # batch profile
    batches: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        batches[r["deactivated_at_utc"][:16]].append(r["id"])

    summary = {
        "n_deactivated": len(rows),
        "verdicts": dict(sorted(vcount.items(), key=lambda kv: -kv[1])),
        "restore_n": len(restores),
        "restore_dollars": round(rest_dollars, 2),
        "stamped_n_ge20_dollars": round(all_stamped_dollars, 2),
        "batches": {k: len(v) for k, v in sorted(batches.items(), key=lambda kv: -len(kv[1]))},
        "illegal_symbol_rows": [r["id"] for r in rows if r["symbol"] in BANNED],
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()