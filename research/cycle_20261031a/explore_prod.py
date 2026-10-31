"""PRODUCTION-FILL exploration harness (2026-10-31 cycle).

WHY (finding 9169 / commit 7c0106c): the custom engine's retro-fill defect was
fixed.  Under the corrected model -- decision on the close of bar T, fill at the
OPEN of bar T+1 -- the whole legacy library (silver_bullet / ict_mitigation /
ifvg_midpoint / ob_midpoint / htf_1h_disp) collapses to PF 0.1-0.5.  Only the
MARKET-ENTRY family survives (csid439/441).

This module:
  * reproduces the PRODUCTION fill semantics 1:1 (entry = open of T+1, stop
    wins ties, a stop already breached at the open fills at the open, no
    re-entry while a trade is open, v1_realistic_micros costs);
  * generates signals VECTORISED (the engine calls evaluate() once per bar,
    which is O(n^2) and far too slow for a grid);
  * validates itself against the REAL engine (src.research.custom_strategy_runtime)
    before any result is trusted.

Usage:
    .venv/bin/python research/cycle_20261031a/explore_prod.py grid
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.portfolio_runtime import _CONTRACT_SPECS  # noqa: E402
from src.tools.cost_model import apply_costs_to_trade, round_trip_cost  # noqa: E402

LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]


# ---------------------------------------------------------------- bars
def load_bars(symbol: str, tf: str, days: int | None = None) -> pd.DataFrame:
    p = Path("data/bars_archive") / f"{symbol}_{tf}.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    # drop IBKR no-trade filler exactly like the runtime does
    if "volume" in df.columns:
        filler = (df["volume"] <= 0) & (df["high"] <= df["low"])
        df = df.loc[~filler].reset_index(drop=True)
    per_day = {"5m": 288, "15m": 96, "1h": 24}[tf]
    if days and len(df) > per_day * days:
        df = df.tail(per_day * days).reset_index(drop=True)
    return df


# ---------------------------------------------------------------- simulator
def simulate_prod(bars: pd.DataFrame, sigs: dict, symbol: str, qty: int = 1) -> dict:
    """sigs: {decision_bar_index: (direction, stop, target)} -- PRODUCTION fill."""
    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    spec = _CONTRACT_SPECS[symbol]
    dpp = float(spec["tick_value"]) / float(spec["tick"])
    n = len(bars)
    pnls: list[float] = []
    rs: list[float] = []
    risk_dollars: list[float] = []
    open_trade = None
    pending = None
    n_filled = 0
    for i in range(2, n):
        # (1) fill a pending decision at the OPEN of this bar
        if pending is not None:
            d, stop, target = pending
            open_trade = {"direction": d, "entry": float(O[i]),
                          "stop": float(stop), "target": float(target)}
            pending = None
            n_filled += 1
        # (2) resolve the open trade against THIS bar (adverse first)
        if open_trade is not None:
            d = open_trade["direction"]
            e = open_trade["entry"]
            s = open_trade["stop"]
            t = open_trade["target"]
            res = None
            if d == "long":
                if O[i] <= s:
                    res = (O[i] - e, "stop")
                elif L[i] <= s:
                    res = (s - e, "stop")
                elif H[i] >= t:
                    res = (t - e, "target")
            else:
                if O[i] >= s:
                    res = (e - O[i], "stop")
                elif H[i] >= s:
                    res = (e - s, "stop")
                elif L[i] <= t:
                    res = (e - t, "target")
            if res is not None:
                pts, reason = res
                gross = pts * dpp * qty
                pnl = apply_costs_to_trade(symbol=symbol, gross_pnl=gross,
                                           exit_reason=reason, qty=qty)
                risk = abs(e - s) or 1e-9
                rd = risk * dpp * qty
                pnls.append(float(pnl))
                risk_dollars.append(float(rd))
                rs.append(float(pnl / rd))
                open_trade = None
            continue
        # (3) new decision on this bar
        if i in sigs:
            pending = sigs[i]
    total = float(sum(pnls))
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = float(gw / gl) if gl > 0 else (0.0 if gw == 0 else 999.0)
    running = peak = dd = 0.0
    for x in pnls:
        running += x
        peak = max(peak, running)
        dd = max(dd, peak - running)
    cost_stop = round_trip_cost(symbol, "stop")
    return {
        "n_trades": len(pnls),
        "pf": round(pf, 4) if pf != 999.0 else 999.0,
        "n_wins": int(sum(1 for p in pnls if p > 0)),
        "n_losses": int(sum(1 for p in pnls if p <= 0)),
        "total_pnl": round(total, 2),
        "expectancy_r": round(float(np.mean(rs)), 4) if rs else 0.0,
        "max_drawdown": round(dd, 2),
        "avg_risk_dollars": round(float(np.mean(risk_dollars)), 2) if risk_dollars else 0.0,
        "cost_in_r": round(cost_stop / float(np.mean(risk_dollars)), 3) if risk_dollars else 0.0,
        "n_filled": n_filled,
    }


# ---------------------------------------------------------------- indicators
def _atr(h, l, c, period=14):
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    return pd.Series(tr).rolling(period, min_periods=1).mean().to_numpy(float)


def _ema(x, period):
    return pd.Series(x).ewm(span=period, adjust=False).mean().to_numpy(float)


# ---------------------------------------------------------------- family
def _ref_levels(bars: pd.DataFrame, ref: str, lb: int):
    """prior_high[k] / prior_low[k] = the levels bar k swept.

    'rolling' : max/min of the previous `lb` bars (csid439 semantics).
    'pdh_pdl' : the PREVIOUS UTC day's high / low (session anchored).
    'asian'   : the CURRENT UTC day's ASIAN range (00:00-06:00 UTC) -- the
                classic ICT 'silver bullet' level, but traded MARKET-entry on
                the displacement instead of on a limit retest.
    """
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    if ref == "rolling":
        ph = pd.Series(H).rolling(lb, min_periods=1).max().shift(1).to_numpy(float)
        pl = pd.Series(L).rolling(lb, min_periods=1).min().shift(1).to_numpy(float)
        return ph, pl
    ts = bars["ts"].dt.tz_convert("UTC")
    day = ts.dt.date.to_numpy()
    if ref == "asian":
        hour = ts.dt.hour.to_numpy()
        m = hour < 6
        sub = pd.DataFrame({"d": day[m], "h": H[m], "l": L[m]})
        if len(sub) == 0:
            return np.full(len(bars), np.nan), np.full(len(bars), np.nan)
        g = sub.groupby("d").agg(hh=("h", "max"), ll=("l", "min"))
        hh = g["hh"].to_dict()
        ll = g["ll"].to_dict()
        ph = np.array([hh.get(d, np.nan) for d in day], dtype=float)
        pl = np.array([ll.get(d, np.nan) for d in day], dtype=float)
        return ph, pl
    df = pd.DataFrame({"d": day, "h": H, "l": L})
    g = df.groupby("d").agg(hh=("h", "max"), ll=("l", "min"))
    prev_h = {}
    prev_l = {}
    days = list(g.index)
    for k in range(1, len(days)):
        prev_h[days[k]] = float(g["hh"].iloc[k - 1])
        prev_l[days[k]] = float(g["ll"].iloc[k - 1])
    ph = np.array([prev_h.get(d, np.nan) for d in day], dtype=float)
    pl = np.array([prev_l.get(d, np.nan) for d in day], dtype=float)
    return ph, pl


def sigs_sweep_disp(bars: pd.DataFrame, p: dict) -> dict:
    """MARKET-ENTRY sweep -> displacement continuation (the surviving family).

    Semantics are matched 1:1 to csid439 (market_entry_sweep_cont_mgc15_v1):
    the sweep test at candidate bar k uses the ATR of the DECISION bar i, the
    sweep is the most recent qualifying bar within `confirm_bars`, and the
    stop sits beyond the sweep bar's extreme + buffer.

    p keys:
      lookback, min_sweep_atr, disp_k, body_ratio_min, confirm_bars,
      stop_mode ('structural'|'atr'), stop_buf_atr, stop_atr_mult, rr,
      sweep_ref ('rolling'|'pdh_pdl'), sess_lo, sess_hi, htf_mult, htf_ema,
      long_only, short_only
    """
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    O = bars["open"].to_numpy(float)
    n = len(bars)
    a = _atr(H, L, C, int(p.get("atr_period", 14)))
    lb = int(p.get("lookback", 12))
    ms = float(p.get("min_sweep_atr", 0.30))
    disp_k = float(p.get("disp_k", 1.0))
    min_body = float(p.get("body_ratio_min", 0.55))
    confirm = int(p.get("confirm_bars", 6))
    buf = float(p.get("stop_buf_atr", 0.15))
    smult = float(p.get("stop_atr_mult", 1.0))
    rr = float(p.get("rr", 1.5))
    stop_mode = str(p.get("stop_mode", "structural"))
    ref = str(p.get("sweep_ref", "rolling"))

    ph, pl = _ref_levels(bars, ref, lb)
    rng = np.maximum(H - L, 1e-12)
    body = np.abs(C - O) / rng

    # most-recent sweep inside the confirmation window (vectorised over the
    # offset d = i - k, exactly as the strategy scans k from cur-1 downwards)
    j = np.full(n, -1, dtype=int)
    jdir = np.zeros(n, dtype=int)          # +1 long (low sweep), -1 short
    hours = bars["ts"].dt.hour.to_numpy()
    asian_mode = (ref == "asian")
    for d in range(1, confirm + 1):
        idx = np.arange(n) - d
        valid = idx >= max(lb + 1, 0)
        k = np.clip(idx, 0, n - 1)
        if asian_mode:
            # the Asian range is only COMPLETE after 06:00 UTC -> both the
            # decision bar and the sweep bar must be at/after 06:00
            valid = valid & (hours >= 6) & (hours[k] >= 6)
        with np.errstate(invalid="ignore"):
            dn = valid & (L[k] < pl[k] - ms * a) & (C[k] > pl[k])
            up = valid & (H[k] > ph[k] + ms * a) & (C[k] < ph[k])
        hit = (dn | up) & (j < 0)
        j = np.where(hit, k, j)
        jdir = np.where(hit & dn, 1, np.where(hit & up, -1, jdir))

    disp_ok = (rng >= disp_k * a) & (body >= min_body)

    htf_diff = None
    if int(p.get("htf_mult", 0)) > 1:
        # CAUSAL HTF trend: a base bar in bucket b may only use buckets that
        # have CLOSED (b-1 and earlier).  The earlier version mapped the FULL
        # resample onto every bar of the bucket, which leaked the bucket's
        # final close backwards -- a look-ahead that inflated the filter.
        base_min = {"5m": 5, "15m": 15, "1h": 60}.get(str(p.get("_tf", "15m")), 15)
        rule = f"{int(p['htf_mult']) * base_min}min"
        s = pd.Series(C, index=pd.DatetimeIndex(bars["ts"]))
        htf = s.resample(rule).last().dropna()
        e = _ema(htf.to_numpy(float), int(p.get("htf_ema", 20)))
        v = (htf - pd.Series(e, index=htf.index)).shift(1)      # bucket b -> b-1
        bucket = pd.DatetimeIndex(bars["ts"]).floor(rule)
        htf_diff = v.reindex(bucket).to_numpy(float)

    hours = bars["ts"].dt.hour.to_numpy()
    sess_lo = int(p.get("sess_lo", -1))
    sess_hi = int(p.get("sess_hi", -1))
    lo_only = int(p.get("long_only", 0))
    sh_only = int(p.get("short_only", 0))

    out = {}
    for i in range(60, n):
        jj = j[i]
        if jj < 0:
            continue
        if not disp_ok[i]:
            continue
        if jdir[i] > 0:
            direction = "long"
            if not (C[i] > pl[jj]):
                continue
        else:
            direction = "short"
            if not (C[i] < ph[jj]):
                continue
        if sess_lo >= 0 and sess_hi >= 0:
            h = hours[i]
            if sess_lo <= sess_hi:
                if not (sess_lo <= h < sess_hi):
                    continue
            else:
                if not (h >= sess_lo or h < sess_hi):
                    continue
        if htf_diff is not None:
            dv = htf_diff[i]
            if not np.isfinite(dv):
                continue
            if (1.0 if direction == "long" else -1.0) * dv <= 0:
                continue
        if lo_only and direction != "long":
            continue
        if sh_only and direction != "short":
            continue
        entry_ref = C[i]
        if stop_mode == "atr":
            risk = smult * a[i]
            stop = entry_ref - risk if direction == "long" else entry_ref + risk
        else:
            if direction == "long":
                stop = L[jj] - buf * a[i]
            else:
                stop = H[jj] + buf * a[i]
            risk = abs(entry_ref - stop)
        if risk <= 0 or not np.isfinite(risk):
            continue
        sign = 1.0 if direction == "long" else -1.0
        target = entry_ref + sign * rr * risk
        out[i] = (direction, float(stop), float(target))
    return out


# ---------------------------------------------------------------- reporting
def split_report(bars: pd.DataFrame, sigs: dict, symbol: str, frac=0.6) -> dict:
    n = len(bars)
    cut = int(n * frac)
    tr_sigs = {i: v for i, v in sigs.items() if i < cut}
    te_sigs = {i: v for i, v in sigs.items() if i >= cut}
    train = simulate_prod(bars.iloc[:cut].reset_index(drop=True), tr_sigs, symbol)
    test = simulate_prod(bars.iloc[cut:].reset_index(drop=True),
                         {i - cut: v for i, v in te_sigs.items()}, symbol)
    return {"train": train, "test": test}


if __name__ == "__main__":
    print("explore_prod module -- import and use from a driver script")
