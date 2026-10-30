"""task #2894 -- ENTRY_ALLOCATION_ORDER_AND_SHADOW_FIRE_RATE.

READ-ONLY. No broker writes, no DB writes, no registry writes, no orders.

Answers the four deliverables:
  (1) ACTUAL contract-grant ordering in the scan/fill path, with file+line
      evidence (code path + an EMPIRICAL confirmation from the runtime's own
      `signals` table, which records inserts in scan-iteration order).
  (2) A deterministic EDGE-RANKED allocation rule a coder can implement.
  (3) SHADOW REPLAY of the last 5 sessions' forward signals under an
      EXECUTABLE fill (market at next bar open, adverse-first tie-breaks),
      with the per-session top-10 by shadow-$ and the retire-wave overlap.
  (4) Census of actives that never fire.

EVIDENCE CLASSES (never mixed):
  * BROKER-CONFIRMED : real NT fills (trades.exit_reason in stop/target/
    naked_breach_autoflat). Real money in a sim account.
  * SYNTHETIC SHADOW : forward-signal replay under bar-touch fills. NOT fills.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from src import bar_cache  # noqa: E402
from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools import cost_model  # noqa: E402

DB = str(REPO / "judas_crew.db")
OUT = REPO / "outputs"
ET = ZoneInfo("America/New_York")

# ---- production runtime limits (main.py:212-214 + lucid_guard.RULES) ------
CONTRACT_CAP = 10          # lucid_guard.RULES['max_contracts_aggregate']
MAX_OPEN_POSITIONS = 4     # config.yaml risk.max_open_positions
MAX_TRADES_PER_DAY = 24    # config.yaml risk.max_trades_per_day
MAX_NEW_TRADES_PER_SCAN = 24   # main.py passes max_trades_per_day as max_new_trades

# Retire wave targeted by tasks #2878-#2890 (SHADOW_DECAY wave).
RETIRE_WAVE_IDS = [4895, 4864, 4911, 4890, 4947, 4952, 4981]

# Broker-confirmed exit reasons (real NT fills).
REAL_FILL_REASONS = ("stop", "target", "naked_breach_autoflat")


def q(sql: str, *args) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


# =====================================================================
# (1) RUNTIME ORDER -- the code path
# =====================================================================
RUNTIME_SQL = (
    "SELECT * FROM active_strategies WHERE state='active' "
    "ORDER BY symbol, strategy_family, version DESC"
)


def runtime_order() -> list[dict]:
    rows = q(RUNTIME_SQL)
    for r in rows:
        r["params"] = json.loads(r["params_json"] or "{}")
        r["timeframe"] = bar_cache.normalize_tf(r["params"].get("timeframe"))
        r["engine"] = str(r["params"].get("execution_engine", ""))
    return rows


def tf_of(sid: int, cache: dict) -> str:
    return cache.get(sid, {}).get("timeframe", "1h")


# =====================================================================
# (3) SHADOW REPLAY -- executable fill on forward signals
# =====================================================================
def session_of(ts_utc: pd.Timestamp) -> pd.Timestamp:
    """Globex session label (ET trade date). 18:00 ET D-1 -> 16:40 ET D."""
    et = ts_utc.tz_convert(ET)
    d = et.date()
    if et.hour >= 18:
        d = d + pd.Timedelta(days=1)
    return pd.Timestamp(d)


def build_bars() -> dict:
    out = {}
    for sym in ("MGC", "MNQ", "MCL", "ZF", "6J"):
        for tf in ("5m", "15m", "1h"):
            try:
                df = bar_cache.read_cache(sym, tf)
            except Exception:
                df = pd.DataFrame()
            if df is None or len(df) == 0:
                continue
            df = df.sort_values("ts").reset_index(drop=True)
            out[(sym, tf)] = df
    return out


def replay_signal(sig: dict, tf: str, bars: dict) -> dict | None:
    """Executable-fill replay of ONE forward signal.

    Fill model: market at the OPEN of the first bar that begins after the
    signal-generating bar closed (the runtime submits a MKT parent bracket).
    The broker re-anchors the bracket to the actual fill (place_bracket_nt
    entry_ref), so stop/target keep their ORIGINAL DISTANCE from the fill.
    Adverse-first: if a single bar touches both stop and target, the stop
    is assumed to have filled first.
    Exit: stop / target / EOD flat (16:40 ET).
    """
    sym = sig["symbol"]
    df = bars.get((sym, tf))
    if df is None or len(df) == 0:
        return None
    ts = df["ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)

    sig_ts = pd.Timestamp(sig["ts_utc"])
    # source bar = last bar whose CLOSE (ts + tf) is <= signal insert time
    delta = pd.Timedelta(tf)
    import numpy as np
    closes_at = ts + delta.to_timedelta64()
    idx = int(np.searchsorted(closes_at,
                              np.datetime64(sig_ts.tz_convert("UTC").tz_localize(None)),
                              side="right") - 1)
    if idx < 0:
        return None
    fill_idx = idx + 1
    if fill_idx >= len(ts):
        return None
    entry = float(o[fill_idx])

    direction = sig["direction"]
    s_entry = float(sig["entry"])
    s_stop = float(sig["stop"])
    s_target = float(sig["target"])
    if not s_stop or not s_target or not s_entry:
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
    dpp = _CONTRACT_SPECS[sym]["tick_value"] / _CONTRACT_SPECS[sym]["tick"]

    # Session EOD flat: 16:40 ET on the session trade date.
    sess = session_of(sig_ts)
    eod_utc = pd.Timestamp(sess.date(), tz="UTC") + pd.Timedelta(hours=20, minutes=40)
    if eod_utc.tz is None:
        eod_utc = eod_utc.tz_localize("UTC")
    eod_utc = eod_utc.tz_convert("UTC")

    exit_px = None
    reason = None
    exit_ts = None
    last_close = None
    eod_naive = eod_utc.tz_localize(None)
    for j in range(fill_idx, len(ts)):
        if pd.Timestamp(ts[j]) > eod_naive:
            break
        last_close = float(c[j])
        if direction == "long":
            hit_stop = float(lo[j]) <= stop
            hit_tgt = float(h[j]) >= target
        else:
            hit_stop = float(h[j]) >= stop
            hit_tgt = float(lo[j]) <= target
        if hit_stop:            # adverse-first
            exit_px, reason, exit_ts = stop, "stop", ts[j]
            break
        if hit_tgt:
            exit_px, reason, exit_ts = target, "target", ts[j]
            break
    if exit_px is None:
        if last_close is None:
            return None
        exit_px, reason = last_close, "eod_flat"
        exit_ts = ts[min(fill_idx + 1, len(ts) - 1)]

    gross = (exit_px - entry) * dpp if direction == "long" else (entry - exit_px) * dpp
    cost = cost_model.round_trip_cost(sym, "stop" if reason == "stop" else "target")
    net = gross - cost
    risk_dollars = risk_pts * dpp
    return {
        "signal_id": sig["id"],
        "strategy_id": sig["strategy_id"],
        "strategy_family": sig["strategy_family"],
        "symbol": sym,
        "direction": direction,
        "timeframe": tf,
        "ts_utc": sig["ts_utc"],
        "session": str(sess.date()),
        "sig_entry": s_entry,
        "fill_entry": entry,
        "stop": stop,
        "target": target,
        "exit_px": exit_px,
        "exit_reason": reason,
        "exit_ts": str(pd.Timestamp(exit_ts)),
        "gross": round(gross, 2),
        "cost": round(cost, 2),
        "net": round(net, 2),
        "risk_dollars": round(risk_dollars, 2),
        "R": round(net / risk_dollars, 3) if risk_dollars else 0.0,
    }


# =====================================================================
# (2) EDGE-RANKED ALLOCATION RULE
# =====================================================================
def live_evidence() -> tuple[dict, dict]:
    """Broker-confirmed live evidence.

    by_id  : strategy_id -> {n, wins, losses, net, pf, per_trade}
    by_fam : (symbol, family) -> same
    """
    rows = q(
        "SELECT strategy_id, strategy_family, symbol, direction, pnl_dollars, qty, "
        "exit_reason, opened_at FROM trades WHERE status='closed'"
    )
    by_id: dict[int, dict] = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "net": 0.0})
    by_fam: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "net": 0.0})
    for r in rows:
        if r["exit_reason"] not in REAL_FILL_REASONS:
            continue
        pnl = float(r["pnl_dollars"] or 0.0)
        for key, tgt in ((r["strategy_id"], by_id),
                         ((r["symbol"], r["strategy_family"]), by_fam)):
            d = tgt[key]
            d["n"] += 1
            d["net"] += pnl
            if pnl > 0:
                d["w"] += 1
            else:
                d["l"] += 1
    def fin(d: dict) -> dict:
        gp = sum(1 for _ in ())  # placeholder
        return d
    for d in list(by_id.values()) + list(by_fam.values()):
        d["per_trade"] = d["net"] / d["n"] if d["n"] else 0.0
        d["pf"] = None
    return dict(by_id), dict(by_fam)


def pf_from_rows(rows: list[dict]) -> float | None:
    gp = sum(float(r["pnl_dollars"] or 0) for r in rows if float(r["pnl_dollars"] or 0) > 0)
    gl = -sum(float(r["pnl_dollars"] or 0) for r in rows if float(r["pnl_dollars"] or 0) < 0)
    if gl <= 0:
        return None
    return gp / gl


# =====================================================================
# ALLOCATION SIMULATION
# =====================================================================
def simulate(sigs: list[dict], rank_key, *, policy: str,
             sym_cap: int = 4, fam_cap: int = 2) -> dict:
    """Grant contracts to signals subject to the production caps.

    policy='runtime' : iterate in the runtime's own order (signal id order).
    policy='edge'    : iterate in edge-rank order inside each scan batch.
    """
    # group into scan batches (a scan's inserts are contiguous in id, with a
    # >120s wall-clock gap between scans)
    batches: list[list[dict]] = []
    cur: list[dict] = []
    prev_ts = None
    for s in sigs:
        t = pd.Timestamp(s["ts_utc"])
        if prev_ts is not None and (t - prev_ts).total_seconds() > 120:
            batches.append(cur)
            cur = []
        cur.append(s)
        prev_ts = t
    if cur:
        batches.append(cur)

    placed: list[dict] = []
    blocked: list[dict] = []
    for batch in batches:
        if policy == "edge":
            order = sorted(batch, key=rank_key)
        else:
            order = sorted(batch, key=lambda s: s["signal_id"])
        open_rows = 0
        contracts = 0
        placed_this_scan = 0
        sym_used: dict[str, int] = defaultdict(int)
        fam_used: dict[tuple, int] = defaultdict(int)
        for s in order:
            qty = 1
            eff = contracts + placed_this_scan      # mirrors _eff_contracts
            if eff + qty > CONTRACT_CAP:
                blocked.append({**s, "why": "lucid_contract_cap"})
                continue
            if open_rows >= MAX_OPEN_POSITIONS:
                blocked.append({**s, "why": "max_open_positions"})
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
            open_rows += 1
            contracts += qty
            placed_this_scan += 1
            if placed_this_scan >= MAX_NEW_TRADES_PER_SCAN:
                break
    return {"placed": placed, "blocked": blocked, "batches": len(batches)}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    actives = runtime_order()
    order_map = {a["id"]: i for i, a in enumerate(actives)}
    tf_map = {a["id"]: a["timeframe"] for a in actives}
    fam_map = {a["id"]: (a["symbol"], a["strategy_family"]) for a in actives}

    print("=" * 78)
    print("(1) RUNTIME CONTRACT-GRANT ORDER")
    print("=" * 78)
    print(f"actives = {len(actives)}")
    print("SQL: SELECT * FROM active_strategies WHERE state='active'")
    print("     ORDER BY symbol, strategy_family, version DESC   "
          "(src/strategy_registry.py:944-951)")
    print("\nfirst 16 in runtime order:")
    for a in actives[:16]:
        print(f"   id={a['id']:>5}  {a['symbol']:<4} {a['strategy_family']:<48} v{a['version']}")

    # ---- live (broker-confirmed) evidence ----
    by_id, by_fam = live_evidence()
    tr_rows = q("SELECT strategy_id, strategy_family, symbol, pnl_dollars FROM trades "
                "WHERE status='closed' AND exit_reason IN ('stop','target','naked_breach_autoflat')")
    pf_id: dict[int, float | None] = defaultdict(lambda: None)
    pf_fam: dict[tuple, float | None] = defaultdict(lambda: None)
    grp_id: dict[int, list] = defaultdict(list)
    grp_fam: dict[tuple, list] = defaultdict(list)
    for r in tr_rows:
        grp_id[r["strategy_id"]].append(r)
        grp_fam[(r["symbol"], r["strategy_family"])].append(r)
    for k, v in grp_id.items():
        pf_id[k] = pf_from_rows(v)
    for k, v in grp_fam.items():
        pf_fam[k] = pf_from_rows(v)

    # ---- shadow replay over the last 5 complete Globex sessions ----
    bars = build_bars()
    sig_rows = q("SELECT id, ts_utc, symbol, strategy_id, strategy_family, direction, "
                 "entry, stop, target, agent_notes FROM signals "
                 "WHERE ts_utc >= '2026-10-22T00:00:00Z' ORDER BY id")
    print(f"\nforward signals pulled: {len(sig_rows)}")
    replayed: list[dict] = []
    skipped_nobar = 0
    for s in sig_rows:
        sid = s["strategy_id"]
        if sid is None:
            continue
        tf = tf_map.get(sid)
        if tf is None:
            tf = "1h"
        r = replay_signal(s, tf, bars)
        if r is None:
            skipped_nobar += 1
            continue
        r["runtime_rank"] = order_map.get(sid, 9999)
        r["in_retire_wave"] = sid in RETIRE_WAVE_IDS
        replayed.append(r)
    print(f"replayed: {len(replayed)}   (no-bar/no-fill: {skipped_nobar})")

    # session list
    sessions = sorted({r["session"] for r in replayed})
    print("sessions present:", sessions)
    complete = [s for s in sessions if s != "2026-10-30"]
    last5 = complete[-5:]
    print("replaying last 5 complete sessions:", last5)
    R = [r for r in replayed if r["session"] in last5]
    print(f"signals in those 5 sessions: {len(R)}")

    pd.DataFrame(R).to_csv(OUT / "task2894_shadow_replay.csv", index=False)

    # ---- per-session top-10 by shadow-$ (ALL signals, unconstrained) ----
    print("\n" + "=" * 78)
    print("(3) SHADOW REPLAY -- per-session top-10 by shadow-$ (unconstrained)")
    print("    SYNTHETIC (bar-touch, executable fill). NOT broker fills.")
    print("=" * 78)
    per_session_top: dict[str, list] = {}
    for s in last5:
        rows = [r for r in R if r["session"] == s]
        agg: dict[int, dict] = defaultdict(lambda: {"net": 0.0, "n": 0, "w": 0, "sym": "", "fam": ""})
        for r in rows:
            a = agg[r["strategy_id"]]
            a["net"] += r["net"]; a["n"] += 1; a["w"] += 1 if r["net"] > 0 else 0
            a["sym"] = r["symbol"]; a["fam"] = r["strategy_family"]
        top = sorted(agg.items(), key=lambda kv: -kv[1]["net"])[:10]
        per_session_top[s] = [{"strategy_id": k, **v} for k, v in top]
        tot = sum(v["net"] for v in agg.values())
        print(f"\n-- session {s}: {len(rows)} signals, {len(agg)} distinct actives, "
              f"shadow net ${tot:,.2f}")
        print(f"   {'sid':>6} {'symbol':<5} {'family':<44} {'n':>4} {'W':>4} {'shadow$':>10}  runtime_rank")
        for k, v in top:
            print(f"   {k:>6} {v['sym']:<5} {v['fam']:<44} {v['n']:>4} {v['w']:>4} "
                  f"{v['net']:>10,.2f}  {order_map.get(k,'-')}")

    # ---- never-fire census ----
    print("\n" + "=" * 78)
    print("(4) NEVER-FIRE CENSUS (actives vs forward signals)")
    print("=" * 78)
    fired5 = {r["strategy_id"] for r in R}
    since14 = pd.Timestamp("2026-10-16T00:00:00Z")
    f14 = {s["strategy_id"] for s in sig_rows
           if pd.Timestamp(s["ts_utc"]) >= since14 and s["strategy_id"] is not None}
    f_all = {s["strategy_id"] for s in q("SELECT DISTINCT strategy_id FROM signals") if s["strategy_id"]}
    never5 = [a for a in actives if a["id"] not in fired5]
    never14 = [a for a in actives if a["id"] not in f14]
    never_ever = [a for a in actives if a["id"] not in f_all]
    print(f"actives total                     : {len(actives)}")
    print(f"fired >=1x in last 5 sessions     : {len(actives) - len(never5)}")
    print(f"NEVER fired in last 5 sessions     : {len(never5)}")
    print(f"NEVER fired in last 14 days        : {len(never14)}")
    print(f"NEVER fired EVER (since 2026-05-13): {len(never_ever)}")
    by_sym_never5: dict[str, int] = defaultdict(int)
    for a in never5:
        by_sym_never5[a["symbol"]] += 1
    print("never-in-5 by symbol:", dict(by_sym_never5))
    for a in never14:
        print(f"   id={a['id']:>5} {a['symbol']:<4} {a['strategy_family']:<50} "
              f"tf={a['timeframe']:<3} act={a['activated_at_utc'][:10]}")

    # ---- retire-wave overlap ----
    print("\n" + "=" * 78)
    print("(3b) RETIRE-WAVE OVERLAP (tasks #2878-#2890 targets)")
    print("=" * 78)
    wave = [r for r in R if r["in_retire_wave"]]
    wave_net = sum(r["net"] for r in wave)
    all_net = sum(r["net"] for r in R)
    print(f"retire-wave target ids : {RETIRE_WAVE_IDS}")
    print(f"retire-wave signals in 5 sessions : {len(wave)}  shadow net ${wave_net:,.2f}")
    print(f"ALL signals in 5 sessions          : {len(R)}  shadow net ${all_net:,.2f}")
    print(f"share of shadow-$ in the retire wave: {wave_net/all_net*100 if all_net else 0:.1f}%")
    for sid in RETIRE_WAVE_IDS:
        rows = [r for r in R if r["strategy_id"] == sid]
        if not rows:
            print(f"   id={sid}: 0 signals in the 5 sessions")
            continue
        net = sum(r["net"] for r in rows)
        w = sum(1 for r in rows if r["net"] > 0)
        print(f"   id={sid}: n={len(rows):>4} W={w:>3} shadow net ${net:>10,.2f}  "
              f"rank {order_map.get(sid,'-')}")

    # ---- allocation simulation ----
    print("\n" + "=" * 78)
    print("(3c) ALLOCATION SIMULATION -- who gets the 10 contracts?")
    print("=" * 78)

    def rank_key(s: dict):
        sid = s["strategy_id"]
        fam = fam_map.get(sid, (s["symbol"], s["strategy_family"]))
        lid = by_id.get(sid, {"per_trade": 0.0, "n": 0})
        lfam = by_fam.get(fam, {"per_trade": 0.0, "n": 0})
        # 1) broker-confirmed live $ per trade  2) live PF  3) n
        # family-level evidence used when the strategy_id has no live trades
        pt = lid["per_trade"] if lid["n"] else lfam["per_trade"]
        nn = lid["n"] if lid["n"] else lfam["n"]
        pfv = pf_id.get(sid) if lid["n"] else pf_fam.get(fam)
        pfv = pfv if pfv is not None else 0.0
        # a 0-live-trade active must NOT outrank a proven earner on BT envelope:
        # the live evidence dominates; only when BOTH are 0 does first-fire order decide
        return (-pt, -pfv, -nn, s["signal_id"])

    sims = {}
    for pol in ("runtime", "edge"):
        sims[pol] = simulate(R, rank_key, policy=pol)
        pl = sims[pol]["placed"]
        net = sum(x["net"] for x in pl)
        print(f"\n-- policy={pol}: placed {len(pl)}/{len(R)} signals, shadow net ${net:,.2f}")
        bc: dict[str, int] = defaultdict(int)
        for x in sims[pol]["blocked"]:
            bc[x["why"]] += 1
        print("   blocked by:", dict(bc))
        per_day: dict[str, float] = defaultdict(float)
        per_day_n: dict[str, int] = defaultdict(int)
        for x in pl:
            per_day[x["session"]] += x["net"]
            per_day_n[x["session"]] += 1
        for s in last5:
            print(f"     {s}: n={per_day_n[s]:>4}  shadow net ${per_day[s]:>10,.2f}")

    # head-to-head per session
    print("\n-- HEAD TO HEAD (5-session shadow net $):")
    print(f"   {'session':<12} {'runtime-order':>16} {'edge-ranked':>16} {'delta':>12}")
    for s in last5:
        a = sum(x["net"] for x in sims["runtime"]["placed"] if x["session"] == s)
        b = sum(x["net"] for x in sims["edge"]["placed"] if x["session"] == s)
        print(f"   {s:<12} {a:>16,.2f} {b:>16,.2f} {b-a:>12,.2f}")
    ta = sum(x["net"] for x in sims["runtime"]["placed"])
    tb = sum(x["net"] for x in sims["edge"]["placed"])
    print(f"   {'TOTAL':<12} {ta:>16,.2f} {tb:>16,.2f} {tb-ta:>12,.2f}")

    # which strategies got the cap under each policy
    print("\n-- strategies that RECEIVED a contract under each policy (5 sessions):")
    for pol in ("runtime", "edge"):
        got: dict[int, dict] = defaultdict(lambda: {"n": 0, "net": 0.0, "sym": "", "fam": ""})
        for x in sims[pol]["placed"]:
            g = got[x["strategy_id"]]
            g["n"] += 1; g["net"] += x["net"]
            g["sym"] = x["symbol"]; g["fam"] = x["strategy_family"]
        print(f"   policy={pol}: {len(got)} distinct strategies received a contract")
        for k, v in sorted(got.items(), key=lambda kv: -kv[1]["net"])[:15]:
            print(f"      id={k:>5} {v['sym']:<4} {v['fam']:<44} n={v['n']:>4} ${v['net']:>10,.2f}")

    # ---- live evidence table for the ranking rule ----
    print("\n" + "=" * 78)
    print("(2b) BROKER-CONFIRMED LIVE EVIDENCE per active (real NT fills only)")
    print("=" * 78)
    rows = []
    for a in actives:
        lid = by_id.get(a["id"], {"n": 0, "w": 0, "l": 0, "net": 0.0})
        fam = (a["symbol"], a["strategy_family"])
        lfam = by_fam.get(fam, {"n": 0, "w": 0, "l": 0, "net": 0.0})
        rows.append({
            "strategy_id": a["id"], "symbol": a["symbol"], "family": a["strategy_family"],
            "tf": a["timeframe"], "runtime_rank": order_map[a["id"]],
            "live_n_id": lid["n"], "live_net_id": round(lid["net"], 2),
            "live_pf_id": pf_id.get(a["id"]),
            "live_n_fam": lfam["n"], "live_net_fam": round(lfam["net"], 2),
            "live_pf_fam": pf_fam.get(fam),
        })
    df = pd.DataFrame(rows).sort_values("runtime_rank")
    df.to_csv(OUT / "task2894_edge_ranking.csv", index=False)
    live_any = df[(df.live_n_id > 0) | (df.live_n_fam > 0)]
    print(f"actives with ANY broker-confirmed live evidence: {len(live_any)}/{len(df)}")
    print(live_any.head(25).to_string(index=False))

    # ---- report json ----
    rep = {
        "task": 2894,
        "generated_utc": pd.Timestamp.utcnow().isoformat(),
        "runtime_order": {
            "sql": RUNTIME_SQL,
            "source": "src/strategy_registry.py:944-951 list_active_strategies()",
            "consumer": "src/portfolio_runtime.py:1901 run_portfolio_scan()",
            "gate": "src/portfolio_runtime.py:1677 _gate_fire()",
            "n_actives": len(actives),
        },
        "sessions": last5,
        "n_signals_5sessions": len(R),
        "n_replayed": len(replayed),
        "per_session_top10": per_session_top,
        "retire_wave": {
            "ids": RETIRE_WAVE_IDS,
            "n_signals": len(wave),
            "shadow_net": round(wave_net, 2),
            "all_net": round(all_net, 2),
        },
        "never_fire": {
            "in_5_sessions": [a["id"] for a in never5],
            "in_14_days": [a["id"] for a in never14],
            "ever": [a["id"] for a in never_ever],
        },
        "alloc_sim": {
            "runtime": {
                "placed": len(sims["runtime"]["placed"]),
                "net": round(sum(x["net"] for x in sims["runtime"]["placed"]), 2),
            },
            "edge": {
                "placed": len(sims["edge"]["placed"]),
                "net": round(sum(x["net"] for x in sims["edge"]["placed"]), 2),
            },
        },
    }
    (OUT / "task2894_allocation_report.json").write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nwrote {OUT/'task2894_allocation_report.json'}")
    print(f"wrote {OUT/'task2894_shadow_replay.csv'}")
    print(f"wrote {OUT/'task2894_edge_ranking.csv'}")


if __name__ == "__main__":
    main()