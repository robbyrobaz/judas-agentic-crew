"""Durable DEEP bar archive — paginated historical backfill for intraday timeframes.

Why this exists
---------------
`src/bar_cache.py` asks IBKR for a single fixed window per timeframe
(`_TF_SPECS`: 5m -> "10 D", 15m -> "20 D", 1h -> "60 D"). A single request for
"20 D"/"30 D" of 5m does NOT fail with a cap error -- IBKR accepts it, then kills
it with its own ~60s internal request timeout and returns **zero bars**
(error 162 "query cancelled"). Measured on MGCZ6, 2026-10-21:

    5m "10 D"  -> 2586 bars  (OK, ~22s)
    5m "20 D"  ->    0 bars  (IBKR internal timeout at 60s)
    5m "30 D"  ->    0 bars  (IBKR internal timeout at 60s)

So the bar_cache ceiling is a REQUEST-SIZE ceiling, not a DATA-EXISTENCE
ceiling. IBKR accepts an explicit `endDateTime`, so the history is reachable by
walking backwards in small anchored chunks and stitching the results.

Verified (research/probe_ibkr_pagination.py, MGCZ6): 10 chunks x 9 days yields
**18,289 bars spanning 92 days** of 5m -- versus the 2,730 bars / 13 days the
cache held.

This module builds that archive durably on disk so research backtests can be
measured over a real window instead of a 13-day fit.

Design notes
------------
* **Chunk size is a tuning knob.** 9 days of 5m is ~2.3k bars and lands inside
  IBKR's request timeout comfortably; larger chunks time out, smaller chunks
  just cost round-trips.
* **Overlap is intentional.** Each chunk asks for `chunk_days` ending at the
  cursor, and the cursor then steps back by `chunk_days`, so adjacent chunks
  share a boundary. De-duplication by timestamp absorbs the overlap.
* **Gaps are tolerated, not fatal.** Weekends/holidays simply return fewer bars.
  An empty chunk usually means we have walked off the end of available history,
  so we stop rather than burn the pacing budget.
* **IBKR paces historical requests.** We sleep between chunks to stay well
  inside the limits; a saturated connection returns empty responses, not errors.
* **This is additive.** The live scanner keeps using `src/bar_cache.py`
  untouched; the archive is consulted by the research backtest path only, so
  nothing about live signal generation changes.

Storage: ``data/bars_archive/{symbol}_{timeframe}.parquet`` (OHLCV + ts, UTC).
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "data" / "bars_archive"

# Bar-size strings per canonical timeframe (mirrors bar_cache._TF_SPECS keys).
BAR_SIZE = {"5m": "5 mins", "15m": "15 mins", "1h": "1 hour"}

# Default chunk width in days per timeframe. 5m is the tightest constraint
# (most bars per day); 15m/1h can take wider chunks comfortably.
DEFAULT_CHUNK_DAYS = {"5m": 9, "15m": 20, "1h": 60}

# Sleep between chunks to respect IBKR historical-data pacing. Without it a
# saturated connection silently returns empty frames instead of erroring.
_CHUNK_SLEEP_S = 2.0

# Per-request ceiling. A request that exceeds this is almost certainly beyond
# IBKR's internal limit; do not wait much longer.
_REQUEST_TIMEOUT_S = 90.0

_BARS_PER_DAY = {"5m": 288, "15m": 96, "1h": 24}


def normalize_tf(tf: str | None) -> str:
    key = str(tf or "1h").strip().lower().replace("-", "").replace(" ", "")
    return {"5m": "5m", "5min": "5m", "5mins": "5m", "5minute": "5m",
            "15m": "15m", "15min": "15m", "15mins": "15m",
            "1h": "1h", "1hr": "1h", "1hour": "1h", "60m": "1h", "h": "1h"}.get(key, "1h")


def archive_path(symbol: str, timeframe: str = "1h") -> Path:
    return ARCHIVE_DIR / f"{symbol.upper()}_{normalize_tf(timeframe)}.parquet"


def read_archive(symbol: str, timeframe: str = "1h", max_days: int | None = None) -> pd.DataFrame:
    """Read the durable archive. Returns an EMPTY DataFrame if not built yet.

    Never raises for a missing archive -- callers fall back to the live cache.
    """
    p = archive_path(symbol, timeframe)
    if not p.exists():
        return _empty_df()
    try:
        df = pd.read_parquet(p)
    except Exception as exc:  # noqa: BLE001
        log.warning("bar_archive.read_failed path=%s err=%s", p, exc)
        return _empty_df()
    if df.empty or "ts" not in df.columns:
        return _empty_df()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
    if max_days:
        per_day = _BARS_PER_DAY.get(normalize_tf(timeframe), 24)
        if len(df) > per_day * max_days:
            df = df.tail(per_day * max_days).reset_index(drop=True)
    return df


def archive_span_days(symbol: str, timeframe: str = "1h") -> int:
    """Calendar days covered by the archive (0 if empty)."""
    df = read_archive(symbol, timeframe)
    if df.empty:
        return 0
    return int((df["ts"].iloc[-1] - df["ts"].iloc[0]).days)


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])


def _write_archive(symbol: str, timeframe: str, df: pd.DataFrame) -> Path:
    """Write the archive ATOMICALLY.

    The archive is written while other processes (the research screens, the
    live scanner's research path) may be READING it.  A plain
    ``df.to_parquet(p)`` truncates the target first, so a concurrent reader can
    observe a half-written file and die with a parquet decode error -- or, worse,
    read a silently truncated series.  Write to a temp file in the SAME
    directory and ``os.replace`` it into place: POSIX rename is atomic, so a
    reader sees either the old file or the new one, never a partial one.
    """
    import os
    import tempfile

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    p = archive_path(symbol, timeframe)
    df = df.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.stem + ".", suffix=".tmp", dir=str(p.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
    return p


def merge_into_archive(symbol: str, timeframe: str, new_rows: list[dict]) -> pd.DataFrame:
    """Merge ``new_rows`` (dicts with ts/open/high/low/close/volume) into the
    archive on disk. Returns the merged, sorted frame."""
    if not new_rows:
        return read_archive(symbol, timeframe)
    new_df = pd.DataFrame(new_rows)
    new_df["ts"] = pd.to_datetime(new_df["ts"], utc=True)
    existing = read_archive(symbol, timeframe)
    if not existing.empty:
        new_df = pd.concat([existing, new_df], ignore_index=True)
    merged = (new_df.sort_values("ts")
              .drop_duplicates(subset=["ts"], keep="last")
              .reset_index(drop=True))
    _write_archive(symbol, timeframe, merged)
    return merged


async def _fetch_chunk(ib, contract, tf: str, chunk_days: int, end_dt) -> list:
    """One anchored historical request. Returns [] on any failure/timeout.

    IBKR rejects anything that is not ``yyyymmdd hh:mm:ss`` (error 10314:
    "End Date/Time: The date, time, or time-zone entered is invalid"), so the
    dashes in an ISO format must be stripped.
    """
    fmt = end_dt.strftime("%Y%m%d %H:%M:%S")
    try:
        return await asyncio.wait_for(
            ib.reqHistoricalDataAsync(
                contract,
                endDateTime=fmt,
                durationStr=f"{int(chunk_days)} D",
                barSizeSetting=BAR_SIZE[tf],
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            ),
            timeout=_REQUEST_TIMEOUT_S,
        ) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("bar_archive.chunk_failed end=%s err=%s: %s", fmt, type(exc).__name__, exc)
        return []


async def backfill_symbol(
    symbol: str,
    timeframe: str = "5m",
    *,
    target_days: int = 120,
    host: str = "127.0.0.1",
    port: int = 4002,
    client_id: int = 4329,
    chunk_days: int | None = None,
    contract_month: str | None = None,
) -> dict[str, Any]:
    """Paginate backwards from now, building the deep archive for one symbol.

    ``contract_month`` (YYYYMM) pins the contract instead of using the
    volume-based roll.  The volume probe is not deterministic across processes
    and can select a thin skip-month (ZF picked ZFM7 = 2027-06, which has NO
    5m history -> HMDS "returned no data"), while the live book trades the
    front contract (ZFZ6 = 2026-12 per active_contracts.json).  The archive
    must be on the same instrument the strategies fire on, so callers pass the
    live contract month explicitly.

    Returns a summary dict (total_bars, span_days, first, last, chunks...).
    """
    from datetime import datetime, timedelta

    tf = normalize_tf(timeframe)
    chunk_days = chunk_days or DEFAULT_CHUNK_DAYS.get(tf, 9)

    # Resolve the SAME contract the live cache trades (highest volume), so the
    # archive is on the same instrument the strategies will fire on.
    sys_path_added = False
    try:
        import sys as _sys
        if "." not in _sys.path:
            _sys.path.insert(0, ".")
            sys_path_added = True
        from src import bar_cache

        ib_mod = __import__("ib_async")
        ib = ib_mod.IB()
        await ib.connectAsync(host, port, clientId=client_id, timeout=20)
        try:
            contract, month, err = await bar_cache._pick_contract(
                ib, bar_cache._CONTRACT_SPECS[symbol.upper()], symbol.upper())
            if err or contract is None:
                return {"error": f"contract_resolve_failed: {err}"}
            if contract_month:
                # Pin the contract the LIVE book trades.
                spec = bar_cache._CONTRACT_SPECS[symbol.upper()]
                forced = ib_mod.Future(symbol=spec["ibkr_symbol"],
                                       exchange=spec["exchange"],
                                       lastTradeDateOrContractMonth=contract_month)
                q = await ib.qualifyContractsAsync(forced)
                if not q or q[0] is None:
                    return {"error": f"contract_override_failed:{contract_month}"}
                contract, month = q[0], contract_month

            seen: dict[str, dict] = {}
            # Seed from whatever is already archived so a resumed backfill
            # doesn't refetch history we already hold.
            existing = read_archive(symbol, tf)
            if not existing.empty:
                for r in existing.to_dict("records"):
                    seen[r["ts"].isoformat()] = r

            cursor = datetime.now()
            chunks: list[dict] = []
            n_chunks = max(1, int(target_days // chunk_days))
            for i in range(n_chunks):
                t0 = time.time()
                bars = await _fetch_chunk(ib, contract, tf, chunk_days, cursor)
                added = 0
                for b in bars:
                    ts = b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date)
                    if ts not in seen:
                        seen[ts] = {
                            "ts": ts,
                            "open": float(b.open), "high": float(b.high),
                            "low": float(b.low), "close": float(b.close),
                            "volume": int(getattr(b, "volume", 0) or 0),
                        }
                        added += 1
                rec = {"chunk": i, "bars_returned": len(bars), "bars_added": added,
                       "cumulative": len(seen), "elapsed_s": round(time.time() - t0, 1)}
                chunks.append(rec)
                if not bars:
                    break
                cursor = cursor - timedelta(days=chunk_days)
                await asyncio.sleep(_CHUNK_SLEEP_S)

            rows = list(seen.values())
            merged = merge_into_archive(symbol, tf, rows)
            span_days = 0
            if not merged.empty:
                span_days = int((merged["ts"].iloc[-1] - merged["ts"].iloc[0]).days)
            return {
                "symbol": symbol, "timeframe": tf, "contract": contract.localSymbol,
                "contract_month": month, "total_bars": int(len(merged)),
                "span_days": span_days,
                "first": str(merged["ts"].iloc[0]) if not merged.empty else None,
                "last": str(merged["ts"].iloc[-1]) if not merged.empty else None,
                "chunks_run": len(chunks),
            }
        finally:
            ib.disconnect()
    finally:
        if sys_path_added:
            import sys as _sys
            try:
                _sys.path.remove(".")
            except ValueError:
                pass


def backtest_bars(symbol: str, timeframe: str = "1h", max_days: int = 120):
    """Bars for BACKTESTING: prefer the deep archive, fall back to the live cache.

    This is the honest-history entry point. If the archive exists for this
    symbol/timeframe it is used (a genuinely wide window); otherwise we fall
    back to `bar_cache.get_bars` which is only ~13 days for 5m — callers should
    treat that narrow case as low-confidence.
    """
    tf = normalize_tf(timeframe)
    df = read_archive(symbol, tf, max_days=max_days)
    if not df.empty:
        return df
    from src import bar_cache
    from src.config import load_config
    cfg = load_config()
    ibkr = cfg.ibkr
    try:
        return bar_cache.get_bars(symbol.upper(), host=ibkr.host, port=ibkr.port,
                                  client_id=int(ibkr.data_client_id) + 9, timeframe=tf)
    except Exception:  # noqa: BLE001
        return _empty_df()