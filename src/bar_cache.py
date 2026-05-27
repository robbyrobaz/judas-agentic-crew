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
import time
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache_1h"
PRICE_CACHE = Path(__file__).resolve().parent.parent / "last_bar_closes.json"
MAX_AGE_HOURS = 1.0

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


async def _pick_contract(ib, spec: dict, symbol: str):
    """Qualify ContFuture then check if the next month is already more liquid.

    Roll rule: if the front-month contract expires within 14 days AND the
    next-month contract exists AND its recent 10-bar average volume > front-month,
    return the next-month contract (volume-based roll).
    """
    from datetime import date, timedelta
    from ib_async import ContFuture, Future

    cont = ContFuture(spec["ibkr_symbol"], exchange=spec["exchange"])
    qualified = await ib.qualifyContractsAsync(cont)
    if not qualified:
        return None, "qualify_failed"

    front = qualified[0]
    expiry_str = front.lastTradeDateOrContractMonth or ""
    if len(expiry_str) < 8:
        return front, None  # can't parse expiry — use as-is

    try:
        exp_date = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
    except ValueError:
        return front, None

    days_to_expiry = (exp_date - date.today()).days
    if days_to_expiry > 14:
        return front, None  # plenty of time, no roll check needed

    # Near expiry — check the next month contract
    next_month_approx = exp_date + timedelta(days=28)
    next_expiry_ym = next_month_approx.strftime("%Y%m")
    try:
        next_cont = Future(
            symbol=spec["ibkr_symbol"],
            lastTradeDateOrContractMonth=next_expiry_ym,
            exchange=spec["exchange"],
        )
        qualified_next = await ib.qualifyContractsAsync(next_cont)
    except Exception:
        qualified_next = []

    if not qualified_next:
        log.info("bar_cache.no_next_contract symbol=%s expiry=%s", symbol, next_expiry_ym)
        return front, None

    next_contract = qualified_next[0]

    # Fetch recent 5-bar snapshot for both and compare volume
    front_snap = await _fetch_bars(ib, front, duration="5 D")
    next_snap = await _fetch_bars(ib, next_contract, duration="5 D")

    front_vol = sum(getattr(b, "volume", 0) or 0 for b in front_snap[-10:])
    next_vol = sum(getattr(b, "volume", 0) or 0 for b in next_snap[-10:])

    if next_vol > front_vol:
        log.info(
            "bar_cache.rolling symbol=%s front=%s(vol=%d,dte=%d) -> next=%s(vol=%d)",
            symbol, front.localSymbol, front_vol, days_to_expiry,
            next_contract.localSymbol, next_vol,
        )
        return next_contract, None
    else:
        log.info(
            "bar_cache.no_roll symbol=%s front=%s(vol=%d,dte=%d) next=%s(vol=%d) — front still dominant",
            symbol, front.localSymbol, front_vol, days_to_expiry,
            next_contract.localSymbol, next_vol,
        )
        return front, None


async def _fetch_async(
    symbols: set[str], host: str, port: int, client_id: int
) -> dict[str, pd.DataFrame]:
    from ib_async import IB
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    out: dict[str, pd.DataFrame] = {}
    try:
        for symbol in sorted(symbols):
            spec = _CONTRACT_SPECS.get(symbol)
            if not spec:
                log.warning("bar_cache.unknown_symbol symbol=%s", symbol)
                continue
            contract, err = await _pick_contract(ib, spec, symbol)
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
            log.info("bar_cache.fetched symbol=%s contract=%s bars=%d", symbol, contract.localSymbol, len(df))
    finally:
        ib.disconnect()
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
