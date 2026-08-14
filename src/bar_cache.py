"""Shared 1H bar cache — one IBKR fetch per hour, every component reads from disk.

Usage:
  from src.bar_cache import get_bars, refresh_cache, read_cache

  # Portfolio scan: refresh everything, then read
  refresh_cache(symbols, host, port, client_id)
  bars = read_cache("MGC")

  # Research/backtesting: just read (always fast)
  bars = read_cache("MGC")

  # Smart fetch: use cache if fresh, hit IBKR only if stale
  bars = get_bars("MGC", host=host, port=port, client_id=client_id)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache_1h"
PRICE_CACHE = Path(__file__).resolve().parent.parent / "last_bar_closes.json"
# Single source of truth for the live ACTIVE contract per symbol, written each
# refresh from the same IBKR resolution the data fetch uses. NT execution reads
# this so it always trades the exact contract we signalled on (no hand-edited
# config months at rolls). {symbol: {contract_month, local, ltd, updated}}.
ACTIVE_CONTRACTS = Path(__file__).resolve().parent.parent / "active_contracts.json"
MAX_AGE_HOURS = 1.0
ACTIVE_MAX_AGE_HOURS = 24.0

# Timeframe support. The detectors are timeframe-agnostic (they consume whatever
# OHLC bars they are handed), so a strategy's timeframe is purely a data-layer
# choice. Each tf has: the IBKR barSizeSetting, how much history to pull, and a
# cache freshness window (a 5m bar is stale after ~5 min, a 1h bar after ~1h).
_TF_SPECS: dict[str, dict[str, Any]] = {
    "5m":  {"bar_size": "5 mins",  "duration": "10 D", "max_age_hours": 5.0 / 60.0},
    "15m": {"bar_size": "15 mins", "duration": "20 D", "max_age_hours": 15.0 / 60.0},
    "1h":  {"bar_size": "1 hour",  "duration": "60 D", "max_age_hours": 1.0},
}
DEFAULT_TF = "1h"

# Accept the many ways a timeframe gets written across config/registry/agents.
_TF_ALIASES = {
    "5m": "5m", "5min": "5m", "5mins": "5m", "5 min": "5m", "5 mins": "5m", "5minute": "5m",
    "15m": "15m", "15min": "15m", "15mins": "15m", "15 min": "15m", "15 mins": "15m",
    "1h": "1h", "1hr": "1h", "1hour": "1h", "1 hour": "1h", "60m": "1h", "60min": "1h", "h": "1h",
}


def normalize_tf(tf: str | None) -> str:
    """Map any timeframe spelling to a canonical key in _TF_SPECS. Unknown -> 1h."""
    if not tf:
        return DEFAULT_TF
    key = str(tf).strip().lower().replace("-", "")
    return _TF_ALIASES.get(key, DEFAULT_TF)

# Futures month codes -> calendar month (for localSymbol parsing fallback).
_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

# (2026-08-14: the active contract is now ALWAYS the highest-volume candidate —
# the old "only probe volume near front expiry" window is gone; it picked thin
# skip-month contracts on gold. _ROLL_WINDOW_DAYS kept only for reference.)
_ROLL_WINDOW_DAYS = 45
# Safety floor: force-roll to the next contract this many days before the
# front's expiry regardless of volume, so we never trade an about-to-expire
# contract (pure volume crossover can lag to ~1 day before expiry).
_ROLL_MIN_DAYS = 4
# Volume comparison uses the last N hours (responsive to the crossover without
# the lag of a full-day sum or the flip-flop risk of a single hour).
_ROLL_VOL_HOURS = 6
# Per-process memo so a symbol's contract is resolved once per scan (not once
# per timeframe). Cleared implicitly each oneshot run.
_ROLL_DECISION: dict[str, tuple] = {}

_CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "MGC": {"ibkr_symbol": "MGC", "exchange": "COMEX"},
    "MNQ": {"ibkr_symbol": "MNQ", "exchange": "CME"},
    "MCL": {"ibkr_symbol": "MCL", "exchange": "NYMEX"},
    "MBT": {"ibkr_symbol": "MBT", "exchange": "CME"},
    "MET": {"ibkr_symbol": "MET", "exchange": "CME"},
    "DX":  {"ibkr_symbol": "DX",  "exchange": "NYBOT"},
    "ZF":  {"ibkr_symbol": "ZF",  "exchange": "CBOT"},
    "6J":  {"ibkr_symbol": "JPY", "exchange": "CME"},
    "ZN":  {"ibkr_symbol": "ZN",  "exchange": "CBOT"},
    "MGC": {"ibkr_symbol": "MGC", "exchange": "COMEX"},
}

# De-duplicate (Python dict last-wins on duplicate keys)
_CONTRACT_SPECS = {
    "MGC": {"ibkr_symbol": "MGC", "exchange": "COMEX"},
    "MNQ": {"ibkr_symbol": "MNQ", "exchange": "CME"},
    "MCL": {"ibkr_symbol": "MCL", "exchange": "NYMEX"},
    "MBT": {"ibkr_symbol": "MBT", "exchange": "CME"},
    "MET": {"ibkr_symbol": "MET", "exchange": "CME"},
    "DX":  {"ibkr_symbol": "DX",  "exchange": "NYBOT"},
    "ZF":  {"ibkr_symbol": "ZF",  "exchange": "CBOT"},
    "6J":  {"ibkr_symbol": "JPY", "exchange": "CME"},
    "ZN":  {"ibkr_symbol": "ZN",  "exchange": "CBOT"},
}


def cache_path(symbol: str, timeframe: str = DEFAULT_TF) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{normalize_tf(timeframe)}.parquet"


def cache_age_hours(symbol: str, timeframe: str = DEFAULT_TF) -> float | None:
    """Return age of cached file in hours, or None if missing."""
    p = cache_path(symbol, timeframe)
    if not p.exists():
        return None
    return (time.time() - p.stat().st_mtime) / 3600.0


def is_fresh(symbol: str, timeframe: str = DEFAULT_TF, max_age_hours: float | None = None) -> bool:
    if max_age_hours is None:
        max_age_hours = _TF_SPECS[normalize_tf(timeframe)]["max_age_hours"]
    age = cache_age_hours(symbol, timeframe)
    return age is not None and age < max_age_hours


def read_cache(symbol: str, timeframe: str = DEFAULT_TF) -> pd.DataFrame:
    """Read cached bars from disk. Raises FileNotFoundError if missing."""
    p = cache_path(symbol, timeframe)
    if not p.exists():
        raise FileNotFoundError(
            f"No {normalize_tf(timeframe)} bar cache for {symbol} — run refresh_cache() first")
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def read_cache_multi(symbols: set[str], timeframe: str = DEFAULT_TF) -> dict[str, pd.DataFrame]:
    """Read multiple symbols from cache, silently skipping missing ones."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            out[sym] = read_cache(sym, timeframe)
        except FileNotFoundError:
            log.warning("bar_cache.missing symbol=%s", sym)
    return out


def _write_price_cache(bars_by_sym: dict[str, pd.DataFrame]) -> None:
    """Write last close for each symbol to last_bar_closes.json."""
    closes: dict[str, Any] = {}
    for sym, df in bars_by_sym.items():
        if df.empty:
            continue
        last = df.iloc[-1]
        ts = str(last["ts"]) if "ts" in df.columns else ""
        closes[sym] = {"close": float(last["close"]), "ts": ts}
    try:
        PRICE_CACHE.write_text(json.dumps(closes))
    except Exception as exc:
        log.warning("bar_cache.price_cache_write_failed: %s", exc)


async def _fetch_bars(ib, contract, duration: str = "60 D", bar_size: str = "1 hour") -> list:
    """Fetch TRADES bars for a qualified contract. Returns empty list on error."""
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=1,
            keepUpToDate=False,
        )
        return bars or []
    except Exception:
        return []


async def _resolve_contract_month(ib, contract, spec: dict) -> str:
    """Authoritative contract month 'YYYYMM' for a resolved contract.

    Uses reqContractDetails().contractMonth — the true delivery month, which is
    distinct from lastTradeDateOrContractMonth (for energies like CL the
    last-trade date falls in the PRIOR calendar month). Falls back to parsing the
    localSymbol month code + ltd year if details are unavailable. Returns '' if
    it cannot be determined.
    """
    from ib_async import Future

    try:
        cds = await ib.reqContractDetailsAsync(
            Future(conId=contract.conId, exchange=spec["exchange"])
        )
        if cds and cds[0].contractMonth and len(cds[0].contractMonth) == 6:
            return cds[0].contractMonth
    except Exception as exc:  # noqa: BLE001
        log.warning("bar_cache.contract_month_details_failed symbol=%s err=%s", spec.get("ibkr_symbol"), exc)

    # Fallback: localSymbol month code (e.g. MGCQ6 -> Q=Aug) + year from ltd.
    ls = getattr(contract, "localSymbol", "") or ""
    ltd = getattr(contract, "lastTradeDateOrContractMonth", "") or ""
    m = re.search(r"([FGHJKMNQUVXZ])(\d{1,2})$", ls)
    if not m or len(ltd) < 6:
        return ""
    month = _MONTH_CODES[m.group(1)]
    year = int(ltd[:4])
    if month < int(ltd[4:6]):  # e.g. Jan (F) contract whose last-trade is prior Dec
        year += 1
    return f"{year:04d}{month:02d}"


async def _recent_volume(ib, contract) -> int:
    """Last few HOURS of traded volume — the roll signal.

    A trailing 24h sum lagged the real crossover by ~7h (it kept averaging in
    the prior day when the front still led). A short recent window tracks the
    'which contract is the market trading now' shift quickly, while still
    smoothing single-hour noise / thin overnight prints (so the pick can't
    flip-flop scan-to-scan the way pure 1-hour volume could)."""
    try:
        bars = await _fetch_bars(ib, contract, duration="2 D", bar_size="1 hour")
        return sum(int(getattr(b, "volume", 0) or 0) for b in bars[-_ROLL_VOL_HOURS:])
    except Exception:  # noqa: BLE001
        return 0


async def _pick_contract(ib, spec: dict, symbol: str):
    """Resolve the ACTIVE contract via an explicit VOLUME-BASED roll.

    Enumerate the real listed contracts, take the nearest-expiry ('front') and
    the next one, and once we're inside the roll window, switch to the next
    contract as soon as it is MORE actively traded than the front (next volume >
    front volume). This rolls exactly when liquidity moves — not on IBKR's
    ContFuture schedule, which can lead/lag the actual volume crossover.

    Returns (contract, contract_month_yyyymm, err).
    """
    from datetime import date, datetime
    from ib_async import Future

    # Per-process memo: the chosen contract is per-SYMBOL, not per-timeframe, so
    # resolve once per scan and reuse for that symbol's 5m/15m/1h refreshes.
    memo = _ROLL_DECISION.get(symbol)
    if memo is not None:
        q = await ib.qualifyContractsAsync(Future(conId=memo[0], exchange=spec["exchange"]))
        if q and q[0] is not None:
            return q[0], memo[1], None

    try:
        cds = await ib.reqContractDetailsAsync(
            Future(symbol=spec["ibkr_symbol"], exchange=spec["exchange"], includeExpired=False))
    except Exception as exc:  # noqa: BLE001
        return None, "", f"details_failed:{exc}"
    rows = []
    for cd in cds or []:
        c = cd.contract
        raw = c.lastTradeDateOrContractMonth or ""
        ltd8 = raw[:8] if len(raw) >= 8 else (raw + "28" if len(raw) == 6 else "")
        if ltd8:
            rows.append((ltd8, cd.contractMonth or "", c))
    rows.sort(key=lambda r: r[0])
    today = date.today().strftime("%Y%m%d")
    live = [r for r in rows if r[0] >= today] or rows
    if not live:
        return None, "", "no_live_contract"

    # ACTIVE = HIGHEST VOLUME, always (Rob's rule, 2026-08-14). The old logic
    # took the nearest-listed expiry and only probed volume inside a roll
    # window near its expiry — which silently picked the THIN skip-month
    # contract on products like gold (nearest-listed = MGC Oct, volume lives
    # in Dec; NT doesn't even carry the Oct expiry). Now: drop anything inside
    # the _ROLL_MIN_DAYS force-roll fence, probe recent volume on the first
    # few remaining candidates, and trade the one that's actually traded.
    def _dte(r) -> int:
        try:
            return (datetime.strptime(r[0], "%Y%m%d").date() - date.today()).days
        except ValueError:
            return 0

    cands = [r for r in live if _dte(r) > _ROLL_MIN_DAYS][:3] or [live[-1]]
    chosen = cands[0]
    if len(cands) > 1:
        vols = [await _recent_volume(ib, r[2]) for r in cands]
        best = max(range(len(cands)), key=lambda i: vols[i])
        chosen = cands[best]
        log.info("bar_cache.volume_pick symbol=%s chose=%s %s",
                 symbol, chosen[2].localSymbol,
                 " ".join(f"{r[2].localSymbol}(vol={v},dte={_dte(r)})"
                          for r, v in zip(cands, vols)))

    contract = chosen[2]
    month = chosen[1] or await _resolve_contract_month(ib, contract, spec)
    _ROLL_DECISION[symbol] = (contract.conId, month)
    return contract, month, None


def _merge_active_contracts(new: dict[str, dict]) -> None:
    """Merge freshly-resolved active contracts into active_contracts.json.

    Merge (not overwrite) so symbols served from cache this round keep their
    last-known active contract.
    """
    try:
        existing = json.loads(ACTIVE_CONTRACTS.read_text()) if ACTIVE_CONTRACTS.exists() else {}
    except Exception:  # noqa: BLE001
        existing = {}
    existing.update(new)
    try:
        ACTIVE_CONTRACTS.write_text(json.dumps(existing, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.warning("bar_cache.active_contracts_write_failed: %s", exc)


def active_contract_month(symbol: str) -> str | None:
    """Live active contract month 'YYYYMM' for a symbol, or None if missing/stale.

    Stale = older than ACTIVE_MAX_AGE_HOURS. Callers MUST treat None as
    'cannot determine the contract' and refuse to guess (do not fall back to a
    hard-coded config month, which silently drifts at every roll)."""
    try:
        data = json.loads(ACTIVE_CONTRACTS.read_text())
    except Exception:  # noqa: BLE001
        return None
    rec = data.get(symbol.upper())
    if not rec:
        return None
    if (time.time() - float(rec.get("updated", 0))) / 3600.0 > ACTIVE_MAX_AGE_HOURS:
        return None
    cm = rec.get("contract_month") or ""
    return cm if len(cm) == 6 else None


def nt_month(symbol: str) -> str | None:
    """NT contract-month label 'MM-YY' for the live active contract, or None."""
    cm = active_contract_month(symbol)
    return f"{cm[4:6]}-{cm[2:4]}" if cm else None


async def _fetch_async(
    symbols: set[str], host: str, port: int, client_id: int, timeframe: str = DEFAULT_TF
) -> dict[str, pd.DataFrame]:
    from ib_async import IB
    tf = normalize_tf(timeframe)
    tf_spec = _TF_SPECS[tf]
    ib = IB()
    # A transient IBKR connect timeout (clientId still in use from a prior scan,
    # gateway hiccup) must NOT crash the whole scan — that loses reconcile + eval
    # for every symbol. Retry once, then degrade gracefully to {} so callers fall
    # back to cached bars. (The scan runs every 5 min, so a skipped fetch self-
    # heals on the next tick.)
    import asyncio as _asyncio
    for _attempt in range(2):
        try:
            await ib.connectAsync(host, port, clientId=client_id, timeout=15)
            break
        except Exception as exc:  # noqa: BLE001
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if _attempt == 0:
                log.warning("bar_cache.connect_retry tf=%s err=%s", tf, exc)
                await _asyncio.sleep(2)
                continue
            log.error("bar_cache.connect_failed tf=%s err=%s — using cached bars this scan", tf, exc)
            return {}
    out: dict[str, pd.DataFrame] = {}
    active: dict[str, dict] = {}
    try:
        for symbol in sorted(symbols):
            # Per-symbol isolation: one symbol's contract/qualify/fetch failure
            # must NEVER abort the whole scan (a DX roll crash on 2026-06-01
            # blinded MNQ/MCL/MGC/MET for ~5h). Warn and skip the bad symbol.
            try:
                spec = _CONTRACT_SPECS.get(symbol)
                if not spec:
                    log.warning("bar_cache.unknown_symbol symbol=%s", symbol)
                    continue
                contract, contract_month, err = await _pick_contract(ib, spec, symbol)
                if err or contract is None:
                    log.warning("bar_cache.qualify_failed symbol=%s err=%s", symbol, err)
                    continue
                bars = await _fetch_bars(ib, contract, duration=tf_spec["duration"], bar_size=tf_spec["bar_size"])
                if not bars:
                    log.warning("bar_cache.no_bars symbol=%s tf=%s contract=%s", symbol, tf, contract.localSymbol)
                    continue
                rows = []
                for b in bars:
                    ts = b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date)
                    rows.append({
                        "ts": pd.to_datetime(ts, utc=True),
                        "open": float(b.open), "high": float(b.high),
                        "low": float(b.low), "close": float(b.close),
                        "volume": int(b.volume or 0),
                    })
                df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
                out[symbol] = df
                if contract_month:
                    active[symbol] = {
                        "contract_month": contract_month,
                        "local": getattr(contract, "localSymbol", ""),
                        "ltd": getattr(contract, "lastTradeDateOrContractMonth", ""),
                        "updated": time.time(),
                    }
                else:
                    log.warning("bar_cache.no_contract_month symbol=%s contract=%s",
                                symbol, contract.localSymbol)
                log.info("bar_cache.fetched symbol=%s tf=%s contract=%s month=%s bars=%d",
                         symbol, tf, contract.localSymbol, contract_month or "?", len(df))
            except Exception as exc:  # noqa: BLE001
                log.error("bar_cache.symbol_failed symbol=%s err=%s", symbol, exc, exc_info=True)
                continue
    finally:
        ib.disconnect()
    if active:
        _merge_active_contracts(active)
    return out


def refresh_cache(
    symbols: set[str],
    host: str,
    port: int,
    client_id: int,
    force: bool = False,
    timeframe: str = DEFAULT_TF,
) -> dict[str, pd.DataFrame]:
    """Fetch fresh bars from IBKR for any stale symbols at the given timeframe.

    Returns the bars_by_sym dict (cached + freshly fetched) for that timeframe.
    Freshness uses the timeframe's own TTL (5m staler faster than 1h). If
    force=True, re-fetches all regardless of age.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tf = normalize_tf(timeframe)

    stale = {s for s in symbols if force or not is_fresh(s, tf)}
    fresh = {s for s in symbols if s not in stale}

    result: dict[str, pd.DataFrame] = {}

    # Load already-fresh symbols from disk immediately
    for sym in fresh:
        try:
            result[sym] = read_cache(sym, tf)
        except FileNotFoundError:
            stale.add(sym)

    if stale:
        log.info("bar_cache.refreshing tf=%s symbols=%s", tf, sorted(stale))
        fetched = asyncio.run(_fetch_async(stale, host, port, client_id, timeframe=tf))
        for sym, df in fetched.items():
            p = cache_path(sym, tf)
            df.to_parquet(p, index=False)
            result[sym] = df
            log.info("bar_cache.wrote path=%s rows=%d", p, len(df))
        # Any stale symbol we couldn't fetch (connect failure / no bars): fall
        # back to its last-known cached bars so the scan still evaluates rather
        # than going blind. The new-bar dedup gate keeps it from re-firing on the
        # unchanged bar, so this is safe.
        for sym in stale - set(fetched):
            try:
                result[sym] = read_cache(sym, tf)
                log.warning("bar_cache.using_stale_cache tf=%s symbol=%s", tf, sym)
            except FileNotFoundError:
                pass
    else:
        log.info("bar_cache.all_fresh tf=%s symbols=%s", tf, sorted(symbols))

    # Price cache is a 1h-only convenience (last close per symbol); only the 1h
    # refresh maintains it so faster timeframes don't churn it.
    if tf == DEFAULT_TF:
        _write_price_cache(result)
    return result


def refresh_cache_pairs(
    pairs: set[tuple[str, str]],
    host: str,
    port: int,
    client_id: int,
    force: bool = False,
) -> dict[tuple[str, str], pd.DataFrame]:
    """Refresh many (symbol, timeframe) pairs, grouped by timeframe so each
    timeframe fetches in one IBKR pass. Returns {(symbol, tf): df}."""
    by_tf: dict[str, set[str]] = {}
    for sym, tf in pairs:
        by_tf.setdefault(normalize_tf(tf), set()).add(sym)
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for tf, syms in by_tf.items():
        got = refresh_cache(syms, host=host, port=port, client_id=client_id, force=force, timeframe=tf)
        for sym, df in got.items():
            out[(sym, tf)] = df
    return out


def get_bars(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 150,
    timeframe: str = DEFAULT_TF,
    max_age_hours: float | None = None,
) -> pd.DataFrame:
    """Get bars for a single symbol/timeframe — from cache if fresh, IBKR if stale."""
    tf = normalize_tf(timeframe)
    if is_fresh(symbol, tf, max_age_hours):
        return read_cache(symbol, tf)
    fetched = refresh_cache({symbol}, host=host, port=port, client_id=client_id, timeframe=tf)
    if symbol in fetched:
        return fetched[symbol]
    raise RuntimeError(f"Failed to fetch bars for {symbol} ({tf})")
