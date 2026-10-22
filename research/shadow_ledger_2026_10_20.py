"""SHADOW_LEDGER_COUNTERFACTUAL_PNL_2026-10-20

READ-ONLY. Reconciles every signal emitted during the CUSHION_EXHAUSTED halt
window (2026-10-12 -> 2026-10-20) against subsequent bars and produces a
counterfactual shadow P&L ledger.

RESOLUTION STATE MACHINE (the important part)
--------------------------------------------
Signals carry `entry` as a PRICE LEVEL to be reached, not a market fill. A
correct counterfactual must therefore:
  1. ARM  the order at the signal bar.
  2. FILL only when price trades THROUGH the entry level in the trade
     direction: long fills on a bar whose LOW <= entry (limit buy),
     short fills on a bar whose HIGH >= entry (limit sell).
     If price never trades the entry level inside FILL_WINDOW bars the
     order is NO_FILL and is excluded from PF entirely.
  3. RESOLVE from the fill bar onward (adversarial): if the fill bar's
     opposite extreme already breaches the stop, it is a LOSS (you cannot
     assume you got filled and then escaped the stop in the same bar).
  4. Anything still open at HOLD_HORIZON bars after fill is EXPIRED (R=0,
     excluded from PF, reported separately).

ASSUMPTION SET (explicit, every number below is conditional on these)
--------------------------------------------------------------------
  A1. Entry = market-limit fill at the signal row's `entry` price.
  A2. Resolution bars = 5m (finest cached series for all 5 legal symbols,
      covers 2026-10-06T22:00Z -> 2026-10-20T15:25Z).
  A3. Mid-bar ambiguity resolved ADVERSARIALLY (both stop and target in one
      bar => LOSS).
  A4. FILL_WINDOW = 144 bars (12h). HOLD_HORIZON = 288 bars (24h) post-fill.
  A5. One shadow trade per (strategy_id, symbol, direction, trigger bar);
      duplicate rows on the same trigger bar are collapsed.
  A6. SYNTHETIC / bar-touch-reconciled. NOT broker fills. Never mixed with
      realized P&L, never presented as an earnings claim.
  A7. Risk per trade = |entry - stop| in price; dollar P&L uses NinjaTrader
      point values (MGC $10, MNQ $2, MCL $10, ZF $1000, 6J $125000 per 1.0
      move) at exactly 1 contract, BEFORE commissions/slippage. Commissions
      are real and non-trivial at these sizes -- see sensitivity note in the
      finding.

Outputs: research/shadow_ledger_2026_10_20.json
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "judas_crew.db"
CACHE = ROOT / "cache_1h"
OUT = ROOT / "research" / "shadow_ledger_2026_10_20.json"

WIN_START = "2026-10-12T00:00:00Z"
WIN_END = "2026-10-20T15:30:00Z"
FILL_WINDOW = 144      # 5m bars the order stays live for a fill
HOLD_HORIZON = 288     # 5m bars the position is held after fill
LEGAL = {"MGC", "MNQ", "MCL", "6J", "ZF"}
BANNED = {"DX", "MET", "MBT"}
PV = {"MGC": 10.0, "MNQ": 2.0, "MCL": 10.0, "ZF": 1000.0, "6J": 125000.0}


def session_et(ts_utc: pd.Timestamp) -> str:
    try:
        et = ts_utc.tz_convert("America/New_York")
    except Exception:
        et = ts_utc
    h = et.hour + et.minute / 60.0
    if h < 4:
        return "Asia"
    if h < 8:
        return "Asia/London"
    if h < 12:
        return "London"
    if h < 13.75:
        return "NY AM"
    return "NY PM"


def load_bars(symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(CACHE / f"{symbol}_5m.parquet")
    df = df.assign(ts=pd.to_datetime(df["ts"], utc=True)).sort_values("ts")
    return df.reset_index(drop=True)


def resolve(row, bars: pd.DataFrame, idx0: int,
            fill_window: int, hold_horizon: int) -> tuple[str, int]:
    """ARM -> FILL -> RESOLVE. Returns (outcome, bars_held)."""
    entry, stop, target = row.entry, row.stop, row.target
    long = row.direction == "long"

    # --- phase 1: arm / fill ------------------------------------------------
    if fill_window > 0:
        fill_end = min(idx0 + fill_window, len(bars))
        if long:
            cands = np.flatnonzero(
                bars["low"].to_numpy()[idx0:fill_end] <= entry)
        else:
            cands = np.flatnonzero(
                bars["high"].to_numpy()[idx0:fill_end] >= entry)
        if not len(cands):
            return "NO_FILL", 0
        fill_idx = idx0 + int(cands[0])
    else:
        # marketable fill: already at market, resolve from the next bar
        fill_idx = idx0
        fill_px = entry
        if (long and fill_px <= stop) or (not long and fill_px >= stop):
            return "INVALID_AT_FILL", 0     # setup already dead when signalled

    # --- phase 2: resolve from the fill bar ---------------------------------
    hi_lim = min(fill_idx + 1 + hold_horizon, len(bars))
    seg_lo = bars["low"].to_numpy()[fill_idx:hi_lim]
    seg_hi = bars["high"].to_numpy()[fill_idx:hi_lim]
    stop_hit = np.flatnonzero(seg_lo <= stop) if long else np.flatnonzero(seg_hi >= stop)
    tgt_hit = np.flatnonzero(seg_hi >= target) if long else np.flatnonzero(seg_lo <= target)
    st = int(stop_hit[0]) if len(stop_hit) else None
    tg = int(tgt_hit[0]) if len(tgt_hit) else None
    if st is not None and tg is not None:
        return ("LOSS", st) if st <= tg else ("WIN", tg)     # adverse tie-break
    if st is not None:
        return "LOSS", st
    if tg is not None:
        return "WIN", tg
    return "EXPIRED", hi_lim - fill_idx


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    sig = pd.read_sql_query(
        "SELECT id, ts_utc, symbol, strategy_id, strategy_family,"
        "       strategy_version, direction, entry, stop, target"
        "  FROM signals"
        f" WHERE ts_utc >= '{WIN_START}' AND ts_utc <= '{WIN_END}'",
        con, parse_dates=["ts_utc"])
    con.close()

    raw_n = len(sig)
    banned_seen = sorted(set(sig.symbol) & BANNED)
    sig = sig[sig.symbol.isin(LEGAL)].dropna(
        subset=["entry", "stop", "target", "direction"]).copy()

    def oriented(r) -> bool:
        return (r.stop < r.entry < r.target) if r.direction == "long" \
            else (r.target < r.entry < r.stop)

    bad_orient = int((~sig.apply(oriented, axis=1)).sum())
    sig = sig[sig.apply(oriented, axis=1)].copy()

    bars = {s: load_bars(s) for s in sorted(set(sig.symbol))}
    tsmap = {s: b["ts"].to_numpy() for s, b in bars.items()}

    rows = []
    for r in sig.itertuples(index=False):
        b, arr = bars[r.symbol], tsmap[r.symbol]
        idx0 = int(pd.Index(arr).searchsorted(r.ts_utc))
        if idx0 >= len(b):
            rows.append(_rec(r, "NO_BAR", 0))
            continue
        outcome, held = resolve(r, b, idx0, FILL_WINDOW, HOLD_HORIZON)
        rows.append(_rec(r, outcome, held))

    res = pd.DataFrame(rows).drop_duplicates(
        subset=["strategy_id", "symbol", "direction", "trigger_ts"])

    risk = (res.entry - res.stop).abs()
    r_win = np.nan_to_num((res.target - res.entry).abs() / risk.replace(0, np.nan),
                          nan=0.0, posinf=0.0)
    traded = res.outcome.isin(["WIN", "LOSS", "EXPIRED"])
    res["r_realized"] = np.where(res.outcome == "WIN", r_win,
                                 np.where(res.outcome == "LOSS", -1.0, 0.0))
    res["usd"] = np.where(traded,
                          res.r_realized * risk * res.symbol.map(PV), 0.0)
    res["usd_win"] = np.where(res.outcome == "WIN", res.usd.abs(), 0.0)
    res["usd_loss"] = np.where(res.outcome == "LOSS", res.usd.abs(), 0.0)

    def agg(g: pd.DataFrame) -> dict:
        w = int((g.outcome == "WIN").sum())
        l = int((g.outcome == "LOSS").sum())
        e = int((g.outcome == "EXPIRED").sum())
        nf = int((g.outcome == "NO_FILL").sum())
        gw, gl = float(g.usd_win.sum()), float(g.usd_loss.sum())
        closed = w + l
        return {
            "n_signals": int(len(g)), "n_traded": closed + e,
            "wins": w, "losses": l, "expired": e, "no_fill": nf,
            "win_rate_closed": round(w / closed, 3) if closed else None,
            "shadow_pf": round(gw / gl, 3) if gl > 0 else None,
            "shadow_usd": round(float(g.usd.sum()), 2),
            "avg_R": round(float(g.r_realized.mean()), 4) if len(g) else None,
            "sum_R": round(float(g.r_realized.sum()), 2),
            "usd_per_signal": round(float(g.usd.mean()), 2) if len(g) else None,
            "fill_rate": round((closed + e) / len(g), 3) if len(g) else None,
        }

    out = {
        "meta": {
            "window": [WIN_START, WIN_END],
            "raw_signals_in_window": raw_n,
            "banned_symbols_seen": banned_seen,
            "bad_stop_target_orientation_dropped": bad_orient,
            "deduped_rows": int(len(res)),
            "fill_window_bars_5m": FILL_WINDOW,
            "hold_horizon_bars_5m": HOLD_HORIZON,
            "resolution": "arm -> fill-through-entry -> adverse resolve",
            "synthetic": True,
        },
        "total": agg(res),
    }
    out["by_symbol"] = {s: agg(g) for s, g in res.groupby("symbol")}
    out["by_session_et"] = {s: agg(g) for s, g in res.groupby("session")}
    out["by_symbol_session"] = {f"{a}|{b}": agg(g)
                                for (a, b), g in res.groupby(["symbol", "session"])}
    out["by_family"] = {f: agg(g) for f, g in res.groupby("family")}
    out["by_strategy"] = {
        int(s): {**agg(g), "symbol": g.symbol.mode().iloc[0],
                 "family": g.family.mode().iloc[0]}
        for s, g in res.groupby("strategy_id")}
    # per-strategy per-symbol (many ids carry >1 symbol across rotations)
    out["by_strategy_symbol"] = {
        f"{int(s)}|{sym}": agg(g) for (s, sym), g in res.groupby(["strategy_id", "symbol"])}

    # --- EXECUTION MODEL C: marketable fill at trigger-bar close -------------
    # Model A/B (above) treat `entry` as a limit level that may never trade.
    # Model C instead assumes the runtime placed a marketable order and got
    # filled at the trigger bar's actual close, keeping the published
    # stop/target levels. This is the "what if we simply traded it" model and
    # is the honest check on whether the negative result is a fill artefact.
    mc = []
    for r in sig.itertuples(index=False):
        b, arr = bars[r.symbol], tsmap[r.symbol]
        i0 = int(pd.Index(arr).searchsorted(r.ts_utc))
        if i0 >= len(b):
            continue
        fill_px = float(b["close"].iloc[i0 - 1])
        # rebuild a resolved row using the marketable fill price
        class _S:  # minimal shim
            pass
        o = _S()
        o.entry, o.stop, o.target, o.direction = fill_px, r.stop, r.target, r.direction
        outcome, _held = resolve(o, b, i0, 0, HOLD_HORIZON)
        mc.append((r.strategy_id, r.symbol, r.direction, str(r.ts_utc), outcome,
                   fill_px, float(r.stop), float(r.target)))
    m = pd.DataFrame(mc, columns=["sid", "symbol", "dir", "ts", "outcome",
                                  "entry", "stop", "target"]).drop_duplicates(
        subset=["sid", "symbol", "dir", "ts"])
    mrk = (m.entry - m.stop).abs()
    mrw = np.nan_to_num((m.target - m.entry).abs() / mrk.replace(0, np.nan),
                        nan=0.0, posinf=0.0)
    m["usd"] = np.where(m.outcome == "WIN", mrw * mrk * m.symbol.map(PV),
                        np.where(m.outcome == "LOSS", -mrk * m.symbol.map(PV), 0.0))
    mw = m[m.outcome == "WIN"].usd.sum()
    ml = m[m.outcome == "LOSS"].usd.abs().sum()
    model_c = {
        "n": int(len(m)),
        "wins": int((m.outcome == "WIN").sum()),
        "losses": int((m.outcome == "LOSS").sum()),
        "expired": int((m.outcome == "EXPIRED").sum()),
        "pf": round(mw / ml, 3) if ml > 0 else None,
        "usd": round(float(m.usd.sum()), 2),
        "win_rate": round((m.outcome == "WIN").sum() /
                          max(1, ((m.outcome == "WIN").sum() + (m.outcome == "LOSS").sum())), 3),
        "by_symbol_pf": {s: round(g[g.outcome == "WIN"].usd.sum() /
                                  max(1e-9, g[g.outcome == "LOSS"].usd.abs().sum()), 3)
                         for s, g in m.groupby("symbol")},
        "by_symbol_usd": {s: round(float(g.usd.sum()), 2) for s, g in m.groupby("symbol")},
    }

    # --- EXECUTION MODEL D: RUNTIME-FAITHFUL ----------------------------------
    # Verified against src/broker/ninjatrader.py: the entry is placed as a
    # MARKET order (line ~886) and the protective stop/target are RE-ANCHORED
    # to the actual fill while PRESERVING the published distances
    # (entry_ref=fire.entry, lines ~915-949). So the faithful counterfactual
    # of what this system would have done is:
    #   fill   = trigger bar close (market fill proxy)
    #   stop   = fill -/+ |published_entry - published_stop|
    #   target = fill +/- |published_target - published_entry|
    # Models B (limit fill at the published level) and C (market fill with the
    # published ABSOLUTE levels) are retained as bounds, not as the primary.
    TICK = {"MGC": 0.1, "MNQ": 0.25, "MCL": 0.01, "ZF": 0.03125, "6J": 0.0000001}

    def _model_d(rows_iter, slip_ticks: float) -> pd.DataFrame:
        out_rows = []
        for r in rows_iter:
            b, arr = bars[r.symbol], tsmap[r.symbol]
            i0 = int(pd.Index(arr).searchsorted(r.ts_utc))
            if i0 >= len(b):
                continue
            sgn = 1.0 if r.direction == "long" else -1.0
            fill = float(b["close"].iloc[i0 - 1]) - sgn * slip_ticks * TICK[r.symbol]
            sd = abs(float(r.entry) - float(r.stop))
            td = abs(float(r.target) - float(r.entry))
            out_rows.append((r.strategy_id, r.symbol, r.strategy_family,
                             r.direction, str(r.ts_utc), fill,
                             fill - sgn * sd, fill + sgn * td,
                             session_et(r.ts_utc)))
        d = pd.DataFrame(out_rows, columns=["sid", "symbol", "family", "dir", "ts",
                                            "fill", "stop", "target", "session"])
        d = d.drop_duplicates(subset=["sid", "symbol", "dir", "ts"])
        lo = np.zeros(len(d)); hi = np.zeros(len(d))
        for sym, g in d.groupby("symbol"):
            b, arr = bars[sym], tsmap[sym]
            gl = np.empty(len(g)); gh = np.empty(len(g)); go = np.empty(len(g), dtype=object)
            for j, r in enumerate(g.itertuples()):
                i0 = int(pd.Index(arr).searchsorted(pd.Timestamp(r.ts)))
                hi_lim = min(i0 + 1 + HOLD_HORIZON, len(b))
                sl = b["low"].to_numpy()[i0:hi_lim]
                sh = b["high"].to_numpy()[i0:hi_lim]
                long = r.dir == "long"
                st = np.flatnonzero(sl <= r.stop) if long else np.flatnonzero(sh >= r.stop)
                tg = np.flatnonzero(sh >= r.target) if long else np.flatnonzero(sl <= r.target)
                si = int(st[0]) if len(st) else None
                ti = int(tg[0]) if len(tg) else None
                if si is not None and ti is not None:
                    go[j] = "LOSS" if si <= ti else "WIN"
                elif si is not None:
                    go[j] = "LOSS"
                elif ti is not None:
                    go[j] = "WIN"
                else:
                    go[j] = "EXPIRED"
            d.loc[g.index, "outcome"] = go
        rk = (d["fill"] - d["stop"]).abs()
        rw = np.nan_to_num((d["target"] - d["fill"]).abs() / rk.replace(0, np.nan),
                           nan=0.0, posinf=0.0)
        d["r_win"] = rw
        d["usd"] = np.where(d.outcome == "WIN", rw * rk * d.symbol.map(PV),
                            np.where(d.outcome == "LOSS", -rk * d.symbol.map(PV), 0.0))
        d["r_realized"] = np.where(d.outcome == "WIN", rw,
                                   np.where(d.outcome == "LOSS", -1.0, 0.0))
        return d

    def _agg(d: pd.DataFrame) -> dict:
        w = int((d.outcome == "WIN").sum()); l = int((d.outcome == "LOSS").sum())
        e = int((d.outcome == "EXPIRED").sum())
        gw = d.loc[d.outcome == "WIN", "usd"].sum()
        gl = d.loc[d.outcome == "LOSS", "usd"].abs().sum()
        return {"n": int(len(d)), "wins": w, "losses": l, "expired": e,
                "win_rate": round(w / (w + l), 3) if (w + l) else None,
                "pf": round(gw / gl, 3) if gl > 0 else None,
                "usd": round(float(d.usd.sum()), 2),
                "sum_R": round(float(d.r_realized.sum()), 2),
                "avg_R": round(float(d.r_realized.mean()), 4),
                "usd_per_signal": round(float(d.usd.mean()), 2)}

    md = _model_d(sig.itertuples(index=False), 0.0)
    md_slip = _model_d(sig.itertuples(index=False), 1.0)
    out["execution_model_D_runtime_faithful"] = {
        "total": _agg(md),
        "with_1tick_adverse_slippage": _agg(md_slip),
        "by_symbol": {s: _agg(g) for s, g in md.groupby("symbol")},
        "by_session_et": {s: _agg(g) for s, g in md.groupby("session")},
    }
    md_out = ROOT / "research" / "shadow_ledger_modelD_rows.csv"
    md.to_csv(md_out, index=False)

    # --- horizon sensitivity -------------------------------------------------
    sens = {}
    for fw, hh in [(72, 144), (144, 288), (288, 288), (144, 576)]:
        rr = []
        for r in sig.itertuples(index=False):
            b, arr = bars[r.symbol], tsmap[r.symbol]
            i0 = int(pd.Index(arr).searchsorted(r.ts_utc))
            if i0 >= len(b):
                continue
            o, _h = resolve(r, b, i0, fw, hh)
            rr.append((r.strategy_id, r.symbol, r.direction, str(r.ts_utc), o,
                       float(r.entry), float(r.stop), float(r.target)))
        d = pd.DataFrame(rr, columns=["sid", "symbol", "dir", "ts", "outcome",
                                      "entry", "stop", "target"]).drop_duplicates(
            subset=["sid", "symbol", "dir", "ts"])
        rk = (d.entry - d.stop).abs()
        rw = np.nan_to_num((d.target - d.entry).abs() / rk.replace(0, np.nan),
                           nan=0.0, posinf=0.0)
        d["usd"] = np.where(d.outcome == "WIN", rw * rk * d.symbol.map(PV),
                           np.where(d.outcome == "LOSS", -rk * d.symbol.map(PV), 0.0))
        w = d[d.outcome == "WIN"].usd.sum()
        l = d[d.outcome == "LOSS"].usd.abs().sum()
        sens[f"fill{fw}_hold{hh}"] = {
            "n": int(len(d)), "wins": int((d.outcome == "WIN").sum()),
            "losses": int((d.outcome == "LOSS").sum()),
            "expired": int((d.outcome == "EXPIRED").sum()),
            "no_fill": int((d.outcome == "NO_FILL").sum()),
            "pf": round(w / l, 3) if l > 0 else None,
            "usd": round(float(d.usd.sum()), 2),
            "sum_R": round(float(np.where(d.outcome == "WIN", rw,
                                          np.where(d.outcome == "LOSS", -1.0, 0.0)).sum()), 2)}
    out["horizon_sensitivity"] = sens
    out["execution_model_C_marketable"] = model_c
    res.to_csv(ROOT / "research" / "shadow_ledger_rows.csv", index=False)
    m.to_csv(ROOT / "research" / "shadow_ledger_rows_modelC.csv", index=False)

    OUT.write_text(json.dumps(out, indent=1, default=str))

    print(json.dumps(out["meta"], indent=1))
    print("\nTOTAL:", json.dumps(out["total"]))
    print("\nSENSITIVITY:")
    for k, v in sens.items():
        print(f"  {k:18s} {json.dumps(v)}")
    print("\nEXECUTION MODEL C (marketable fill at trigger close):")
    print("  ", json.dumps(model_c))
    print("\nEXECUTION MODEL D (RUNTIME-FAITHFUL: market fill, distance-re-anchored SL/TP):")
    print("  no slippage :", json.dumps(_agg(md)))
    print("  1-tick slip :", json.dumps(_agg(md_slip)))
    for s_, g in md.groupby("symbol"):
        print(f"    {s_:4s} {json.dumps(_agg(g))}")
    for s_, g in md.groupby("session"):
        print(f"    {s_:12s} {json.dumps(_agg(g))}")
    print("\nBY SYMBOL:")
    for k, v in out["by_symbol"].items():
        print(f"  {k:4s} {json.dumps(v)}")
    print("\nBY SESSION ET:")
    for k, v in out["by_session_et"].items():
        print(f"  {k:12s} {json.dumps(v)}")


def _rec(r, outcome, held) -> dict:
    return {
        "id": r.id, "strategy_id": r.strategy_id, "symbol": r.symbol,
        "family": r.strategy_family, "direction": r.direction,
        "entry": float(r.entry), "stop": float(r.stop), "target": float(r.target),
        "trigger_ts": str(r.ts_utc), "outcome": outcome, "bars_held": held,
        "session": session_et(r.ts_utc),
    }


if __name__ == "__main__":
    main()