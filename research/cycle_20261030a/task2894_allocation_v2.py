"""task #2894 v2 -- allocation order + shadow fire rate.

READ-ONLY. No broker/DB/registry writes.

Adds over v1:
  * PROPER discrete-event allocation sim: positions persist across scans,
    contracts are released when the position closes (stop/target/EOD).
  * TWO fill models:
      MKT    -- market at the next bar open (WHAT THE RUNTIME ACTUALLY DOES:
                place_bracket_nt sends a MKT parent bracket at a derived
                limit level).
      STRICT -- limit fill only if a later bar actually trades through the
                signal's derived entry level (the #2860 / a760c855
                methodology used by the SHADOW_DECAY retire wave).
  * OUT-OF-SAMPLE ranking test: rank on the FIRST 2 sessions, trade the
    LAST 3 with that ranking.
  * Duplicate-setup census: how many of the ~1000 signals/day are the SAME
    setup claimed by several actives.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src import bar_cache  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools import cost_model  # noqa: E402

DB = str(REPO / "judas_crew.db")
OUT = REPO / "outputs"
ET = ZoneInfo("America/New_York")

CONTRACT_CAP = 10
MAX_OPEN_POSITIONS = 4
MAX_TRADES_PER_DAY = 24

RETIRE_WAVE_IDS = [4895, 4864, 4911, 4890, 4947, 4952, 4981]
REAL_FILL_REASONS = ("stop", "target", "naked_breach_autoflat")
CROSSCHECK_FROM = "2026-10-12T00:00:00Z"
CROSSCHECK_TO = "2026-10-27T23:59:59Z"


def q(sql, *a):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, a).fetchall()]
    finally:
        con.close()


RUNTIME_SQL = ("SELECT * FROM active_strategies WHERE state='active' "
               "ORDER BY symbol, strategy_family, version DESC")


def session_of(ts_utc: pd.Timestamp) -> pd.Timestamp:
    et = ts_utc.tz_convert(ET)
    d = et.date()
    if et.hour >= 18:
        d = d + pd.Timedelta(days=1)
    return pd.Timestamp(d)


def build_bars():
    out = {}
    for sym in ("MGC", "MNQ", "MCL", "ZF", "6J"):
        for tf in ("5m", "15m", "1h"):
            try:
                df = bar_cache.read_cache(sym, tf)
            except Exception:
                continue
            if df is None or len(df) == 0:
                continue
            df = df.sort_values("ts").reset_index(drop=True)
            out[(sym, tf)] = df
    return out


class BarSet:
    def __init__(self, df: pd.DataFrame, tf: str):
        self.tf = tf
        self.ts = df["ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        self.o = df["open"].to_numpy(dtype=float)
        self.h = df["high"].to_numpy(dtype=float)
        self.lo = df["low"].to_numpy(dtype=float)
        self.c = df["close"].to_numpy(dtype=float)
        self.close_at = self.ts + pd.Timedelta(tf).to_timedelta64()

    def source_index(self, sig_ts: pd.Timestamp) -> int:
        naive = np.datetime64(sig_ts.tz_convert("UTC").tz_localize(None))
        return int(np.searchsorted(self.close_at, naive, side="right") - 1)


def replay(sig, tf, bars, model):
    sym = sig["symbol"]
    bs = bars.get((sym, tf))
    if bs is None:
        return None
    sig_ts = pd.Timestamp(sig["ts_utc"])
    i = bs.source_index(sig_ts)
    if i < 0:
        return None
    direction = sig["direction"]
    s_entry = float(sig["entry"])
    s_stop = float(sig["stop"])
    s_target = float(sig["target"])
    if not s_stop or not s_target or not s_entry:
        return None
    dpp = _CONTRACT_SPECS[sym]["tick_value"] / _CONTRACT_SPECS[sym]["tick"]
    sess = session_of(sig_ts)
    eod_naive = (pd.Timestamp(sess.date(), tz="UTC")
                 + pd.Timedelta(hours=20, minutes=40)).tz_localize(None)

    fill_idx = None
    entry = None
    if model == "mkt":
        fill_idx = i + 1
        if fill_idx >= len(bs.ts):
            return None
        entry = float(bs.o[fill_idx])
    else:
        for j in range(i + 1, len(bs.ts)):
            if pd.Timestamp(bs.ts[j]) > eod_naive:
                break
            if direction == "long":
                if float(bs.lo[j]) <= s_entry:
                    fill_idx, entry = j, s_entry
                    break
            else:
                if float(bs.h[j]) >= s_entry:
                    fill_idx, entry = j, s_entry
                    break
        if fill_idx is None:
            return None

    if direction == "long":
        stop = entry - (s_entry - s_stop)
        target = entry + (s_target - s_entry)
    else:
        stop = entry + (s_stop - s_entry)
        target = entry - (s_entry - s_target)
    risk_pts = abs(entry - stop)
    if risk_pts <= 0:
        return None

    exit_px, reason, exit_idx = None, None, fill_idx
    last_close = None
    for j in range(fill_idx, len(bs.ts)):
        if pd.Timestamp(bs.ts[j]) > eod_naive:
            break
        last_close = float(bs.c[j])
        if direction == "long":
            hit_stop = float(bs.lo[j]) <= stop
            hit_tgt = float(bs.h[j]) >= target
        else:
            hit_stop = float(bs.h[j]) >= stop
            hit_tgt = float(bs.lo[j]) <= target
        if hit_stop:
            exit_px, reason, exit_idx = stop, "stop", j
            break
        if hit_tgt:
            exit_px, reason, exit_idx = target, "target", j
            break
    if exit_px is None:
        if last_close is None:
            return None
        exit_px, reason = last_close, "eod_flat"
        exit_idx = min(fill_idx + 1, len(bs.ts) - 1)

    gross = (exit_px - entry) * dpp if direction == "long" else (entry - exit_px) * dpp
    cost = cost_model.round_trip_cost(sym, "stop" if reason == "stop" else "target")
    net = gross - cost
    risk_dollars = risk_pts * dpp
    return {
        "signal_id": sig["id"], "strategy_id": sig["strategy_id"],
        "strategy_family": sig["strategy_family"], "symbol": sym,
        "direction": direction, "timeframe": tf, "ts_utc": sig["ts_utc"],
        "session": str(sess.date()), "sig_entry": s_entry, "fill_entry": entry,
        "stop": stop, "target": target, "exit_px": exit_px, "exit_reason": reason,
        "fill_ts": str(pd.Timestamp(bs.ts[fill_idx])),
        "exit_ts": str(pd.Timestamp(bs.ts[exit_idx])),
        "gross": round(gross, 2), "cost": round(cost, 2), "net": round(net, 2),
        "risk_dollars": round(risk_dollars, 2),
        "R": round(net / risk_dollars, 3) if risk_dollars else 0.0,
        "model": model,
    }


def live_evidence():
    rows = q("SELECT strategy_id, strategy_family, symbol, pnl_dollars FROM trades "
             "WHERE status='closed' AND exit_reason IN ('stop','target','naked_breach_autoflat')")
    by_id = defaultdict(lambda: {"n": 0, "net": 0.0, "gp": 0.0, "gl": 0.0})
    by_fam = defaultdict(lambda: {"n": 0, "net": 0.0, "gp": 0.0, "gl": 0.0})
    for r in rows:
        p = float(r["pnl_dollars"] or 0)
        for key, tgt in ((r["strategy_id"], by_id),
                         ((r["symbol"], r["strategy_family"]), by_fam)):
            d = tgt[key]
            d["n"] += 1
            d["net"] += p
            if p > 0:
                d["gp"] += p
            else:
                d["gl"] += -p
    out = {}
    for src in (by_id, by_fam):
        for k, d in src.items():
            d["pf"] = (d["gp"] / d["gl"]) if d["gl"] > 0 else None
            d["per_trade"] = d["net"] / d["n"] if d["n"] else 0.0
            out[k] = d
    return out, out


def simulate(sigs, rank_key, policy, sym_cap=4, fam_cap=2):
    batches, cur, prev = [], [], None
    for s in sigs:
        t = pd.Timestamp(s["ts_utc"])
        if prev is not None and (t - prev).total_seconds() > 120:
            batches.append(cur)
            cur = []
        cur.append(s)
        prev = t
    if cur:
        batches.append(cur)

    open_pos = []
    placed, blocked = [], []
    trades_today = defaultdict(int)
    for batch in batches:
        batch_fill = min(pd.Timestamp(s["fill_ts"]) for s in batch)
        open_pos = [p for p in open_pos if pd.Timestamp(p["exit_ts"]) > batch_fill]
        contracts_open = sum(p["qty"] for p in open_pos)
        placed_this_scan = 0
        sym_used = defaultdict(int)
        fam_used = defaultdict(int)
        order = (sorted(batch, key=rank_key) if policy == "edge"
                 else sorted(batch, key=lambda s: s["signal_id"]))
        for s in order:
            qty = 1
            eff = contracts_open + placed_this_scan
            if eff + qty > CONTRACT_CAP:
                blocked.append({**s, "why": "lucid_contract_cap"})
                continue
            if len(open_pos) >= MAX_OPEN_POSITIONS:
                blocked.append({**s, "why": "max_open_positions"})
                continue
            if trades_today[s["session"]] >= MAX_TRADES_PER_DAY:
                blocked.append({**s, "why": "max_trades_per_day"})
                continue
            if policy == "edge":
                sk = (s["symbol"], s["strategy_family"])
                if sym_used[s["symbol"]] + qty > sym_cap:
                    blocked.append({**s, "why": "per_symbol_cap"})
                    continue
                if fam_used[sk] + qty > fam_cap:
                    blocked.append({**s, "why": "per_family_cap"})
                    continue
                sym_used[s["symbol"]] += qty
                fam_used[sk] += qty
            placed.append(s)
            open_pos.append({"exit_ts": s["exit_ts"], "qty": qty})
            trades_today[s["session"]] += 1
            placed_this_scan += 1
    return {"placed": placed, "blocked": blocked, "batches": len(batches)}


def agg_by_strategy(rows):
    agg = defaultdict(lambda: {"n": 0, "net": 0.0, "w": 0, "l": 0, "sym": "", "fam": "",
                               "nets": []})
    for r in rows:
        a = agg[r["strategy_id"]]
        a["n"] += 1
        a["net"] += r["net"]
        a["nets"].append(r["net"])
        a["w" if r["net"] > 0 else "l"] += 1
        a["sym"] = r["symbol"]
        a["fam"] = r["strategy_family"]
    for a in agg.values():
        gp = sum(x for x in a["nets"] if x > 0)
        gl = -sum(x for x in a["nets"] if x < 0)
        a["pf"] = gp / gl if gl > 0 else None
    return agg


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    actives = q(RUNTIME_SQL)
    order_map = {a["id"]: i for i, a in enumerate(actives)}
    tf_map, fam_map = {}, {}
    for a in actives:
        p = json.loads(a["params_json"] or "{}")
        tf_map[a["id"]] = bar_cache.normalize_tf(p.get("timeframe"))
        fam_map[a["id"]] = (a["symbol"], a["strategy_family"])
    live_id, live_fam = live_evidence()

    raw = q("SELECT id, ts_utc, symbol, strategy_id, strategy_family, direction, "
            "entry, stop, target FROM signals WHERE ts_utc >= '2026-10-12T00:00:00Z' "
            "ORDER BY id")
    print(f"forward signals since 2026-10-12: {len(raw)}")
    bars = {k: BarSet(v, k[1]) for k, v in build_bars().items()}

    rows = []
    for s in raw:
        sid = s["strategy_id"]
        if sid is None:
            continue
        tf = tf_map.get(sid, "1h")
        for model in ("mkt", "strict"):
            r = replay(s, tf, bars, model)
            if r:
                r["runtime_rank"] = order_map.get(sid, 9999)
                rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "task2894_shadow_replay_v2.csv", index=False)
    print(f"replayed rows: {len(df)}")

    sessions_all = sorted(df[df.model == "mkt"]["session"].unique())
    last5 = [s for s in sessions_all if s != "2026-10-30"][-5:]
    print("sessions:", sessions_all)
    print("last 5 complete:", last5)

    M = df[(df.model == "mkt") & (df.session.isin(last5))].copy()
    S = df[(df.model == "strict") & (df.session.isin(last5))].copy()

    print("\n" + "=" * 78)
    print("FILL MODEL COMPARISON (5 sessions, SYNTHETIC bar-touch evidence)")
    print("=" * 78)
    for name, D in (("MKT   (what the runtime actually does)", M),
                    ("STRICT limit (the #2860/a760c855 method)", S)):
        if not len(D):
            print(f"{name}: no fills")
            continue
        n = len(D)
        gp = float(D[D.net > 0].net.sum())
        gl = float(-D[D.net < 0].net.sum())
        print(f"{name}: n={n:>5} W={int((D.net>0).sum()):>4} L={int((D.net<0).sum()):>4} "
              f"net=${D.net.sum():>12,.2f} per-trade=${D.net.sum()/n:>7,.2f} "
              f"PF={gp/gl if gl else 0:.3f} netR={D.R.sum():>7.1f} "
              f"eod_flat={(D.exit_reason=='eod_flat').mean():.1%}")

    j = (M.groupby("signal_id")
         .agg(net_mkt=("net", "sum"))
         .join(S.groupby("signal_id").agg(net_strict=("net", "sum"),
                                         n_strict=("net", "size")),
               how="left").fillna({"net_strict": 0.0, "n_strict": 0}))
    unfilled = int((j.n_strict == 0).sum())
    print(f"\nMKT vs STRICT over {len(j)} signal ids: mkt net ${j.net_mkt.sum():,.2f} "
          f"vs strict net ${j.net_strict.sum():,.2f}")
    print(f"derived entry level NEVER reached by price: {unfilled} "
          f"({unfilled/max(1,len(j)):.1%}) -> runtime's market order fills those worse")

    print("\n" + "=" * 78)
    print("PER-SESSION TOP-10 BY SHADOW-$ (MKT model, unconstrained)")
    print("=" * 78)
    tops = {}
    for s in last5:
        rr = M[M.session == s].to_dict("records")
        agg = agg_by_strategy(rr)
        tops[s] = [{"strategy_id": k, **{kk: vv for kk, vv in v.items() if kk != "nets"}}
                   for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["net"])[:10]]
        tot = sum(v["net"] for v in agg.values())
        print(f"\n-- {s}: {len(rr)} signals, {len(agg)} actives, shadow net ${tot:,.2f}")
        print(f"   {'sid':>6} {'sym':<4} {'family':<46} {'n':>4} {'W':>4} {'shadow$':>10} {'rank':>5}")
        for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["net"])[:10]:
            print(f"   {k:>6} {v['sym']:<4} {v['fam']:<46} {v['n']:>4} {v['w']:>4} "
                  f"{v['net']:>10,.2f} {order_map.get(k,'-'):>5}")

    print("\n" + "=" * 78)
    print("DUPLICATE-SETUP CENSUS")
    print("=" * 78)
    dup_rows = tot_sig = 0
    for s in last5:
        rr = M[M.session == s].copy()
        tot_sig += len(rr)
        rr["k"] = (rr.symbol + "|" + rr.timeframe + "|" + rr.direction + "|"
                   + rr.fill_ts + "|" + rr.sig_entry.round(4).astype(str))
        g = rr.groupby("k").size()
        red = int((g - 1).clip(lower=0).sum())
        dup_rows += red
        print(f"   {s}: {len(rr):>5} signals -> {len(g):>5} distinct setups "
              f"({int((g>1).sum())} claimed by >1 active, {red} redundant claims)")
    print(f"   TOTAL: {tot_sig} signals -> {tot_sig-dup_rows} distinct setups "
          f"({dup_rows} redundant claims = {dup_rows/tot_sig:.1%})")

    print("\n" + "=" * 78)
    print("OUT-OF-SAMPLE RANKING TEST  (train = first 2 sessions, test = last 3)")
    print("=" * 78)
    train, test = last5[:2], last5[2:]
    Mt = M[M.session.isin(train)].to_dict("records")
    Mo = M[M.session.isin(test)].to_dict("records")
    train_agg = agg_by_strategy(Mt)
    print(f"train {train}: {len(Mt)} signals, {len(train_agg)} actives")

    def key_shadow(s):
        a = train_agg.get(s["strategy_id"])
        if not a or a["n"] == 0:
            return (1, 0.0, 0.0, 0, s["signal_id"])
        return (0, -a["net"], -(a["pf"] or 0), -a["n"], s["signal_id"])

    def key_live(s):
        sid = s["strategy_id"]
        fam = fam_map.get(sid, (s["symbol"], s["strategy_family"]))
        d = live_id.get(sid)
        dfam = live_fam.get(fam)
        if d and d["n"]:
            return (-d["per_trade"], -(d["pf"] or 0), -d["n"], s["signal_id"])
        if dfam and dfam["n"]:
            return (-dfam["per_trade"], -(dfam["pf"] or 0), -dfam["n"], s["signal_id"])
        return (1, 0.0, 0.0, 0, s["signal_id"])

    policies = {
        "runtime (as-is)": lambda s: (s["signal_id"],),
        "edge by TRAIN shadow-$": key_shadow,
        "edge by LIVE broker-confirmed": key_live,
    }
    test_runs = {}
    print(f"\nTEST window {test}: {len(Mo)} signals")
    print(f"   {'policy':<34} {'placed':>7} {'shadow net $':>15} {'$/trade':>10}")
    for pname, kf in policies.items():
        sim = simulate(Mo, kf, "edge" if pname != "runtime (as-is)" else "runtime")
        pl = sim["placed"]
        test_runs[pname] = sim
        net = sum(x["net"] for x in pl)
        print(f"   {pname:<34} {len(pl):>7} {net:>15,.2f} "
              f"{(net/len(pl) if pl else 0):>10,.2f}")
        bc = defaultdict(int)
        for b in sim["blocked"]:
            bc[b["why"]] += 1
        print(f"        blocked: {dict(bc)}")

    print("\n   who received contracts (TEST window):")
    for pname, sim in test_runs.items():
        agg = agg_by_strategy(sim["placed"])
        print(f"   -- {pname}")
        for k, v in sorted(agg.items(), key=lambda kv: -kv[1]["net"])[:8]:
            print(f"      id={k:>5} {v['sym']:<4} {v['fam']:<44} n={v['n']:>4} ${v['net']:>10,.2f}")

    # ---- retire wave overlap ----
    print("\n" + "=" * 78)
    print("RETIRE-WAVE OVERLAP (tasks #2878-#2890 targets) — 5 sessions")
    print("=" * 78)
    wave = M[M.strategy_id.isin(RETIRE_WAVE_IDS)]
    allnet = M.net.sum()
    wavenet = wave.net.sum()
    print(f"retire-wave ids {RETIRE_WAVE_IDS}")
    print(f"wave signals {len(wave)}  wave shadow net ${wavenet:,.2f}   "
          f"book net ${allnet:,.2f}   share {wavenet/allnet*100 if allnet else 0:.1f}%")
    for sid in RETIRE_WAVE_IDS:
        r = wave[wave.strategy_id == sid]
        if not len(r):
            print(f"   id={sid}: 0 signals in the 5 sessions")
            continue
        print(f"   id={sid}: n={len(r):>4} W={int((r.net>0).sum()):>3} "
              f"net ${r.net.sum():>10,.2f}  PF="
              f"{(r[r.net>0].net.sum()/-r[r.net<0].net.sum() if (r.net<0).any() else 0):.2f}")

    # ---- cross-check against the #2860 methodology window ----
    print("\n" + "=" * 78)
    print("CROSS-CHECK vs the #2860 / a760c855 SHADOW_DECAY numbers")
    print(f"   window {CROSSCHECK_FROM[:10]}..{CROSSCHECK_TO[:10]}, STRICT limit fill")
    print("=" * 78)
    Sc = df[(df.model == "strict") & (df.ts_utc >= "2026-10-12T00:00:00Z")
            & (df.ts_utc <= "2026-10-27T23:59:59Z")]
    stated = {4895: ("PF 0.33", 39), 4864: ("PF 0.40", 61), 4911: ("PF 0.27", 33),
              4890: ("PF 0.52", 24), 4947: ("PF 0.30", 35), 4952: ("PF 0.30", 35),
              4981: ("PF 0.50", 24)}
    for sid, (pf_s, n_s) in stated.items():
        r = Sc[Sc.strategy_id == sid]
        if not len(r):
            print(f"   id={sid}: no replayed signals")
            continue
        gp = r[r.net > 0].net.sum()
        gl = -r[r.net < 0].net.sum()
        print(f"   id={sid}: mine n={len(r):>4} PF={gp/gl if gl else 0:.2f} "
              f"net=${r.net.sum():>9,.2f} netR={r.R.sum():>6.1f}   "
              f"| task-stated {pf_s} n={n_s}")

    # ---- never-fire census ----
    print("\n" + "=" * 78)
    print("NEVER-FIRE CENSUS")
    print("=" * 78)
    fired5 = set(M.strategy_id)
    allsig = {s["strategy_id"] for s in q("SELECT DISTINCT strategy_id FROM signals")
              if s["strategy_id"]}
    sig14 = {s["strategy_id"] for s in q(
        "SELECT DISTINCT strategy_id FROM signals WHERE ts_utc>='2026-10-16T00:00:00Z'")
        if s["strategy_id"]}
    never5 = [a for a in actives if a["id"] not in fired5]
    never14 = [a for a in actives if a["id"] not in sig14]
    neverever = [a for a in actives if a["id"] not in allsig]
    print(f"actives                        : {len(actives)}")
    print(f"fired >=1x in last 5 sessions  : {len(actives)-len(never5)}")
    print(f"never fired in last 5 sessions  : {len(never5)}")
    print(f"never fired in last 14 days    : {len(never14)}")
    print(f"never fired EVER               : {len(neverever)}")
    for a in never5:
        print(f"   id={a['id']:>5} {a['symbol']:<4} {a['strategy_family']:<50} "
              f"tf={bar_cache.normalize_tf(json.loads(a['params_json'] or '{}').get('timeframe')):<3} "
              f"act={a['activated_at_utc'][:10]}")

    # ---- write report ----
    rep = {
        "task": 2894,
        "runtime_order": {
            "sql": RUNTIME_SQL,
            "source": "src/strategy_registry.py:944-951 list_active_strategies()",
            "consumer": "src/portfolio_runtime.py:1901 run_portfolio_scan()",
            "gate": "src/portfolio_runtime.py:1677 _gate_fire()",
            "n_actives": len(actives),
            "first_10": [{"id": a["id"], "symbol": a["symbol"],
                          "family": a["strategy_family"], "version": a["version"]}
                         for a in actives[:10]],
        },
        "sessions": last5,
        "fill_models": {
            "mkt": {"n": len(M), "net": round(M.net.sum(), 2),
                    "pf": round(float(M[M.net > 0].net.sum() / -M[M.net < 0].net.sum()), 3)},
            "strict": {"n": len(S), "net": round(S.net.sum(), 2),
                       "pf": round(float(S[S.net > 0].net.sum() / -S[S.net < 0].net.sum()), 3)},
        },
        "per_session_top10": tops,
        "duplicate_setups": {"signals": tot_sig, "distinct": tot_sig - dup_rows,
                             "redundant": dup_rows},
        "oos_test": {k: {"placed": len(v["placed"]),
                         "net": round(sum(x["net"] for x in v["placed"]), 2)}
                     for k, v in test_runs.items()},
        "retire_wave": {"ids": RETIRE_WAVE_IDS, "n": len(wave),
                        "net": round(wavenet, 2), "book_net": round(allnet, 2)},
        "never_fire": {"in_5_sessions": [a["id"] for a in never5],
                       "in_14_days": [a["id"] for a in never14],
                       "ever": [a["id"] for a in neverever]},
    }
    (OUT / "task2894_report_v2.json").write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nwrote {OUT/'task2894_report_v2.json'}")
    print(f"wrote {OUT/'task2894_shadow_replay_v2.csv'}")


if __name__ == "__main__":
    main()
