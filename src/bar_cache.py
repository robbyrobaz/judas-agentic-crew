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

# Futures month codes -> calendar month (for localSymbol parsing fallback).
_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}

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


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_1h.parquet"


def cache_age_hours(symbol: str) -> float | None:
    """Return age of cached file in hours, or None if missing."""
    p = cache_path(symbol)
    if not p.exists():
        return None
    return (time.time() - p.stat().st_mtime) / 3600.0


def is_fresh(symbol: str, max_age_hours: float = MAX_AGE_HOURS) -> bool:
    age = cache_age_hours(symbol)
    return age is not None and age < max_age_hours


def read_cache(symbol: str) -> pd.DataFrame:
    """Read cached bars from disk. Raises FileNotFoundError if missing."""
    p = cache_path(symbol)
    if not p.exists():
        raise FileNotFoundError(f"No bar cache for {symbol} — run refresh_cache() first")
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.sort_values("ts").reset_index(drop=True)


def read_cache_multi(symbols: set[str]) -> dict[str, pd.DataFrame]:
    """Read multiple symbols from cache, silently skipping missing ones."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            out[sym] = read_cache(sym)
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


async def _fetch_bars(ib, contract, duration: str = "60 D") -> list:
    """Fetch 1H TRADES bars for a qualified contract. Returns empty list on error."""
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 hour",
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


async def _pick_contract(ib, spec: dict, symbol: str):
    """Resolve the current ACTIVE contract via IBKR's continuous future.

    ContFuture follows real liquidity, so it rolls correctly for every cycle
    (quarterly ES/NQ/DX, monthly CL, bi-monthly gold) with no month arithmetic.
    The previous +28-day next-month step landed on non-existent months for
    quarterly contracts (the 202607 'no security definition' noise) and never
    actually rolled — ContFuture already did the job underneath it.

    Returns (contract, contract_month_yyyymm, err). contract_month is '' if it
    could not be resolved (the contract is still usable for data; execution will
    loud-skip rather than guess a month).
    """
    from ib_async import ContFuture

    cont = ContFuture(spec["ibkr_symbol"], exchange=spec["exchange"])
    qualified = await ib.qualifyContractsAsync(cont)
    if not qualified or qualified[0] is None:
        return None, "", "qualify_failed"

    front = qualified[0]
    contract_month = await _resolve_contract_month(ib, front, spec)
    return front, contract_month, None


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
    symbols: set[str], host: str, port: int, client_id: int
) -> dict[str, pd.DataFrame]:
    from ib_async import IB
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
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
                bars = await _fetch_bars(ib, contract, duration="60 D")
                if not bars:
                    log.warning("bar_cache.no_bars symbol=%s contract=%s", symbol, contract.localSymbol)
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
                log.info("bar_cache.fetched symbol=%s contract=%s month=%s bars=%d",
                         symbol, contract.localSymbol, contract_month or "?", len(df))
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
) -> dict[str, pd.DataFrame]:
    """Fetch fresh bars from IBKR for any stale symbols, write to cache_1h/.

    Returns the full bars_by_sym dict (cached + freshly fetched).
    If force=True, re-fetches all regardless of age.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    stale = {s for s in symbols if force or not is_fresh(s)}
    fresh = {s for s in symbols if s not in stale}

    result: dict[str, pd.DataFrame] = {}

    # Load already-fresh symbols from disk immediately
    for sym in fresh:
        try:
            result[sym] = read_cache(sym)
        except FileNotFoundError:
            stale.add(sym)

    if stale:
        log.info("bar_cache.refreshing symbols=%s", sorted(stale))
        fetched = asyncio.run(_fetch_async(stale, host, port, client_id))
        for sym, df in fetched.items():
            p = cache_path(sym)
            df.to_parquet(p, index=False)
            result[sym] = df
            log.info("bar_cache.wrote path=%s rows=%d", p, len(df))
    else:
        log.info("bar_cache.all_fresh symbols=%s", sorted(symbols))

    _write_price_cache(result)
    return result


def get_bars(
    symbol: str,
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 150,
    max_age_hours: float = MAX_AGE_HOURS,
) -> pd.DataFrame:
    """Get bars for a single symbol — from cache if fresh, IBKR if stale."""
    if is_fresh(symbol, max_age_hours):
        return read_cache(symbol)
    fetched = refresh_cache({symbol}, host=host, port=port, client_id=client_id)
    if symbol in fetched:
        return fetched[symbol]
    raise RuntimeError(f"Failed to fetch bars for {symbol}")
