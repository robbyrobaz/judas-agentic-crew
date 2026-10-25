"""Registrar gate-4 admissibility check for pending candidates.

Applies the team's OWN standing evidence rules to each pending candidate:
  (1) #2871 one_sentence_rule: envelope must come from a fair DECISION-TIME entry
      restamp, NOT from run_custom_backtest (retro-fill artifact).
  (2) finding 47e2eeca sec.6 / sec.5.4: a v1_realistic_micros stamp is not
      admissible evidence unless it charges the OBSERVED entry divergence.
      Triage screen = per-symbol p50 adverse ticks; p90 = tail (kills ~83%).
  (3) mandatory 4 gates: PF>=1.3, n>=20, E[R]>0, dollar-denominated + stamped.

READ-ONLY. Opens judas_crew.db in mode=ro.
"""
import json
import sqlite3
import sys

DB = "judas_crew.db"

# finding 47e2eeca sec.5.1 - observed adverse entry divergence, in TICKS
P50_TICKS = {"MGC": 21.5, "MNQ": 24.0, "MCL": 7.0, "ZF": 1.1, "6J": 1.75}
P90_TICKS = {"MGC": 234.5, "MNQ": 323.4, "MCL": 42.0, "ZF": 2.4, "6J": 12.9}

# finding 47e2eeca sec.5.1 - tick_value ($/tick/contract)
TICK_VALUE = {"MGC": 1.00, "MNQ": 0.50, "MCL": 1.00, "ZF": 7.8125, "6J": 6.25}


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    ids = [int(a) for a in sys.argv[1:]] or [7376, 7377, 7378, 7379, 7380, 7381]
    out = []
    for cid in ids:
        row = con.execute(
            "SELECT id,symbol,strategy_family,params_json,metrics_json,rationale "
            "FROM strategy_candidates WHERE id=?", (cid,)
        ).fetchone()
        if row is None:
            continue
        p = json.loads(row["params_json"])
        m = json.loads(row["metrics_json"])
        sym = row["symbol"]
        dpp = float(p.get("dollars_per_point", 0) or 0)
        tick = float(p.get("tick", 0) or 0)
        tv = TICK_VALUE[sym]
        n = int(float(m.get("n_trades", 0) or 0))
        w = int(float(m.get("n_wins", 0) or 0))
        l = int(float(m.get("n_losses", 0) or 0))
        pf = float(m.get("profit_factor", m.get("pf", 0)) or 0)
        er = float(m.get("expectancy_r", 0) or 0)
        pnl = float(m.get("total_pnl", 0) or 0)
        cm = m.get("cost_model")
        ev = str(m.get("evidence", ""))
        retro = "run_custom_backtest" in ev
        has_mode = "entry_price_mode" in m or "entry_price_mode" in p

        # gross win / gross loss decomposition from PF + net
        gl = pnl / (pf - 1.0) if pf > 1.0 else float("nan")
        gp = pf * gl
        rec = {
            "id": cid, "symbol": sym, "family": row["strategy_family"],
            "n": n, "pf": round(pf, 3), "expectancy_r": round(er, 4),
            "total_pnl": round(pnl, 2), "cost_model": cm,
            "gate1_pf_ge_1.3": pf >= 1.3,
            "gate2_n_ge_20": n >= 20,
            "gate3_er_gt_0": er > 0,
            "gate4_dollar_stamped": cm == "v1_realistic_micros" and pnl != 0,
            "evidence_is_retro_run_custom_backtest": retro,
            "has_entry_price_mode": has_mode,
            "dollars_per_point": dpp, "tick": tick, "tick_value": tv,
        }
        # charge the observed entry divergence on top of the retro fill
        for label, ticks in (("p50", P50_TICKS[sym]), ("p90", P90_TICKS[sym])):
            extra = (ticks - 1.0) * tv
            tot = extra * n
            gp2 = gp - tot
            gl2 = gl + tot
            rec[f"{label}_extra_per_trade_$"] = round(extra, 4)
            rec[f"{label}_total_charge_$"] = round(tot, 2)
            rec[f"{label}_net_$"] = round(pnl - tot, 2)
            rec[f"{label}_PF"] = round(gp2 / gl2, 3) if gl2 > 0 else None
            rec[f"{label}_survives"] = bool(pnl - tot > 0 and gl2 > 0 and gp2 / gl2 >= 1.3)
        rec["GATE_C_decision_time_restamp"] = not retro and has_mode
        out.append(rec)
    print(json.dumps(out, indent=2, default=str))
    con.close()


if __name__ == "__main__":
    main()
