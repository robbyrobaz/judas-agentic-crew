"""Multi-strategy paper portfolio runtime seeded from workshop research."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from ib_async import ContFuture, Future, IB, LimitOrder, MarketOrder, StopOrder

from src import bar_cache
from src.db.models import init_db
from src.research.agent_tools import get_nt_positions  # noqa: F401
from src.strategy_registry import list_active_strategies
from src.tools.judas_detector import run_judas_detection_rich

log = logging.getLogger(__name__)

_STATUS_DIR = Path(__file__).resolve().parent.parent / "outputs"


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_BLOCKED_INSTRUMENTS_FILE = _STATUS_DIR / "blocked_instruments.json"
_NT_REJECT_BLOCK_THRESHOLD = 2          # consecutive NT entry rejections before skipping
_NT_REJECT_COOLDOWN_S = 6 * 3600        # auto-retry the instrument after this (NT config may be fixed)


def _load_blocked() -> dict:
    try:
        return json.loads(_BLOCKED_INSTRUMENTS_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_blocked(d: dict) -> None:
    try:
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        _BLOCKED_INSTRUMENTS_FILE.write_text(json.dumps(d, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.warning("blocked_instruments.json write failed: %s", exc)


def _is_instrument_blocked(symbol: str) -> bool:
    """True if this symbol is in its auto-skip cooldown after repeated NT entry
    rejections (e.g. 'No risk settings defined' on a freshly-rolled contract).
    Self-clears after the cooldown so it resumes if the NT config is fixed.
    Disable with JUDAS_AUTO_SKIP_REJECTED=0."""
    import os
    import time
    if os.environ.get("JUDAS_AUTO_SKIP_REJECTED") == "0":
        return False
    rec = _load_blocked().get(symbol.upper())
    return bool(rec and rec.get("blocked_until_epoch", 0) > time.time())


def _record_entry_rejection(symbol: str) -> None:
    """Count an NT entry rejection; block the symbol once it hits the threshold."""
    import time
    sym = symbol.upper()
    d = _load_blocked()
    rec = d.get(sym, {"count": 0})
    rec["count"] = int(rec.get("count", 0)) + 1
    rec["last_reject_iso"] = _utc_now_iso()
    if rec["count"] >= _NT_REJECT_BLOCK_THRESHOLD:
        rec["blocked_until_epoch"] = time.time() + _NT_REJECT_COOLDOWN_S
        rec["blocked_until_iso"] = _utc_now_iso()
        log.warning("nt.instrument_auto_blocked symbol=%s after %s consecutive entry rejections — "
                    "skipping ~%sh (likely an NT risk-settings/config gap; define it in the NT GUI)",
                    sym, rec["count"], _NT_REJECT_COOLDOWN_S // 3600)
    d[sym] = rec
    _save_blocked(d)


def _clear_entry_rejection(symbol: str) -> None:
    """A successful entry clears the symbol's rejection streak + any block."""
    sym = symbol.upper()
    d = _load_blocked()
    if sym in d:
        del d[sym]
        _save_blocked(d)


def _as_int(v: Any, default: int = 0) -> int:
    """Tolerant int() for strategy params. A malformed/fractional value
    (e.g. stop_buffer_ticks='0.5' from a bad promoter) must NOT crash the
    entire scan via ValueError — round it and move on. Jun-18 fix."""
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


_CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "MGC": {"ibkr_symbol": "MGC", "exchange": "COMEX", "tick": 0.10, "tick_value": 1.0},
    "MNQ": {"ibkr_symbol": "MNQ", "exchange": "CME", "tick": 0.25, "tick_value": 0.5},
    "MCL": {"ibkr_symbol": "MCL", "exchange": "NYMEX", "tick": 0.01, "tick_value": 1.0},
    "MBT": {"ibkr_symbol": "MBT", "exchange": "CME", "tick": 5.0, "tick_value": 0.5},
    "MET": {"ibkr_symbol": "MET", "exchange": "CME", "tick": 0.50, "tick_value": 0.05},
    "DX": {"ibkr_symbol": "DX", "exchange": "NYBOT", "tick": 0.005, "tick_value": 5.0},
    "ZF": {"ibkr_symbol": "ZF", "exchange": "CBOT", "tick": 0.0078125, "tick_value": 7.8125},
    "6J": {"ibkr_symbol": "JPY", "exchange": "CME", "tick": 0.0000005, "tick_value": 6.25},
}


def _ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def _rsi(c: pd.Series, p: int = 14) -> pd.Series:
    d = c.diff()
    u = d.clip(lower=0).ewm(alpha=1 / p, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / p, adjust=False).mean()
    return 100 - 100 / (1 + u / dn.replace(0, pd.NA))


def _atr(bars: pd.DataFrame, p: int = 14) -> pd.Series:
    h, l, c = bars["high"], bars["low"], bars["close"]
    tr = pd.concat([(h - l), (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.rolling(p).mean()


def _workshop_seed_path() -> Path:
    return Path(__file__).parent.parent / "knowledge_base" / "buffet.yaml"


def load_seed_buffet() -> list[dict[str, Any]]:
    with open(_workshop_seed_path()) as f:
        return yaml.safe_load(f)["strategies"]


def _find_seed_strategy(strategy_name: str) -> dict[str, Any] | None:
    for row in load_seed_buffet():
        if row.get("id") == strategy_name:
            return row
    return None


@dataclass
class ActiveFire:
    strategy_id: int
    strategy_name: str
    strategy_family: str
    strategy_version: int
    symbol: str
    direction: str
    entry: float
    stop: float
    target: float
    qty: int
    rationale: str
    features: dict[str, Any]


def _evaluate_rsi(bars: pd.DataFrame, params: dict[str, Any]) -> tuple[str, float, float, float, dict[str, Any]] | None:
    period = _as_int(params.get("period", 14))
    lo = float(params.get("lo_thr", 30))
    hi = float(params.get("hi_thr", 70))
    target_r = float(params.get("target_r", 2.0))
    sa = float(params.get("stop_atr_mult", 1.5))
    if len(bars) < period + 30:
        return None
    rsi = _rsi(bars["close"], period)
    a = _atr(bars).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    last, prev = rsi.iloc[-1], rsi.iloc[-2]
    if pd.isna(last) or pd.isna(prev):
        return None
    if last > lo and prev <= lo:
        direction = "long"
    elif last < hi and prev >= hi:
        direction = "short"
    else:
        return None
    entry = float(bars["close"].iloc[-1])
    stop_dist = sa * float(a)
    stop = entry - stop_dist if direction == "long" else entry + stop_dist
    target = entry + target_r * stop_dist if direction == "long" else entry - target_r * stop_dist
    return direction, entry, stop, target, {"rsi": float(last), "atr": float(a)}


def _evaluate_ma_cross(bars: pd.DataFrame, params: dict[str, Any]) -> tuple[str, float, float, float, dict[str, Any]] | None:
    fast = _as_int(params.get("fast", 12))
    slow = _as_int(params.get("slow", 26))
    target_r = float(params.get("target_r", 2.0))
    sa = float(params.get("stop_atr_mult", 1.5))
    if len(bars) < slow + 5:
        return None
    f = _ema(bars["close"], fast)
    s = _ema(bars["close"], slow)
    a = _atr(bars).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    cross_up = f.iloc[-1] > s.iloc[-1] and f.iloc[-2] <= s.iloc[-2]
    cross_dn = f.iloc[-1] < s.iloc[-1] and f.iloc[-2] >= s.iloc[-2]
    if not (cross_up or cross_dn):
        return None
    direction = "long" if cross_up else "short"
    entry = float(bars["close"].iloc[-1])
    stop_dist = sa * float(a)
    stop = entry - stop_dist if direction == "long" else entry + stop_dist
    target = entry + target_r * stop_dist if direction == "long" else entry - target_r * stop_dist
    return direction, entry, stop, target, {"fast": float(f.iloc[-1]), "slow": float(s.iloc[-1]), "atr": float(a)}


def _evaluate_bollinger(bars: pd.DataFrame, params: dict[str, Any]) -> tuple[str, float, float, float, dict[str, Any]] | None:
    period = _as_int(params.get("period", 20))
    n_std = float(params.get("n_std", 2.0))
    target_r = float(params.get("target_r", 2.0))
    sa = float(params.get("stop_atr_mult", 1.5))
    if len(bars) < period + 5:
        return None
    ma = bars["close"].rolling(period).mean()
    sd = bars["close"].rolling(period).std()
    upper = ma + n_std * sd
    lower = ma - n_std * sd
    a = _atr(bars).iloc[-1]
    if pd.isna(a) or a <= 0:
        return None
    last, prev = bars.iloc[-1], bars.iloc[-2]
    long_signal = prev["close"] < lower.iloc[-2] and last["close"] > lower.iloc[-1]
    short_signal = prev["close"] > upper.iloc[-2] and last["close"] < upper.iloc[-1]
    if not (long_signal or short_signal):
        return None
    direction = "long" if long_signal else "short"
    entry = float(last["close"])
    stop_dist = sa * float(a)
    stop = entry - stop_dist if direction == "long" else entry + stop_dist
    target = entry + target_r * stop_dist if direction == "long" else entry - target_r * stop_dist
    return direction, entry, stop, target, {"upper": float(upper.iloc[-1]), "lower": float(lower.iloc[-1]), "atr": float(a)}


def _evaluate_pair(bars_by_sym: dict[str, pd.DataFrame], strategy_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    seed = _find_seed_strategy(strategy_name)
    if not seed:
        return []
    a, b = seed["symbols"]
    ba = bars_by_sym.get(a)
    bb = bars_by_sym.get(b)
    if ba is None or bb is None:
        return []
    window = _as_int(params["window"])
    z_entry = float(params["z_entry"])
    z_exit = float(params["z_exit"])
    z_stop = float(params["z_stop"])
    df = pd.concat({"a": ba.set_index("ts")["close"], "b": bb.set_index("ts")["close"]}, axis=1).dropna()
    if len(df) < window + 5:
        return []
    if (df["a"] <= 0).any() or (df["b"] <= 0).any():
        return []
    log_a = pd.Series(np.log(df["a"]), index=df.index)
    log_b = pd.Series(np.log(df["b"]), index=df.index)
    beta = (log_a.rolling(window).cov(log_b) / log_b.rolling(window).var()).fillna(1.0)
    spread = log_a - beta * log_b
    mu = spread.rolling(window).mean()
    sd = spread.rolling(window).std()
    z = (spread - mu) / sd
    z_now = float(z.iloc[-1])
    if not pd.notna(z_now) or abs(z_now) < z_entry:
        return []
    if z_now <= -z_entry:
        directions = [(a, "long"), (b, "short")]
    else:
        directions = [(a, "short"), (b, "long")]
    legs = []
    for sym, direction in directions:
        bars = bars_by_sym[sym]
        atr = _atr(bars).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return []
        entry = float(bars["close"].iloc[-1])
        stop_dist = float(atr)
        stop = entry - stop_dist if direction == "long" else entry + stop_dist
        target = entry + 1.5 * stop_dist if direction == "long" else entry - 1.5 * stop_dist
        legs.append(
            {
                "symbol": sym,
                "direction": direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "features": {"pair_id": strategy_name, "pair_symbol": f"{a}/{b}", "z_score": z_now, "z_exit": z_exit, "z_stop": z_stop},
            }
        )
    return legs


def _prior_day_levels(bars: pd.DataFrame) -> tuple[float, float] | None:
    if bars.empty:
        return None
    history = bars.copy()
    history["session_date"] = pd.to_datetime(history["ts"], utc=True).dt.date.astype(str)
    latest_date = history["session_date"].iloc[-1]
    prior = history[history["session_date"] < latest_date]
    if prior.empty:
        return None
    prior_date = prior["session_date"].max()
    day = prior[prior["session_date"] == prior_date]
    return float(day["high"].max()), float(day["low"].min())


def evaluate_active_strategy(active: dict[str, Any], bars_by_sym: dict[str, pd.DataFrame]) -> list[ActiveFire]:
    params = active["params"]
    engine = str(params.get("execution_engine", "judas_native"))
    strategy_name = str(params.get("strategy_name", f"strategy_{active['id']}"))
    qty = _as_int(params.get("qty", 1))

    fires: list[ActiveFire] = []
    if engine == "judas_native":
        symbol = str(active["symbol"]).upper()
        bars = bars_by_sym.get(symbol)
        if bars is None:
            return fires
        levels = _prior_day_levels(bars)
        if not levels:
            return fires
        prior_high, prior_low = levels
        spec = _CONTRACT_SPECS[symbol]
        det = run_judas_detection_rich(
            symbol=symbol,
            bars_df=bars.tail(_as_int(params.get("detector_lookback_bars", 120))).copy(),
            prior_high=prior_high,
            prior_low=prior_low,
            tick_size=float(spec["tick"]),
            confirmation_bars=_as_int(params.get("confirmation_bars", 4)),
            pivot_length=_as_int(params.get("pivot_length", 2)),
            target_r=float(params.get("target_r", 2.0)),
            stop_buffer_ticks=_as_int(params.get("stop_buffer_ticks", 2)),
            min_sweep_ticks=_as_int(params.get("min_sweep_ticks", 3)),
            dollar_per_point=float(spec["tick_value"] / spec["tick"]),
        )
        if not det.get("pattern_found"):
            return fires
        # Quality filters — applied after pattern_found so params are respected
        _disp = det.get("displacement") or {}
        if _disp.get("strength", 999.0) < float(params.get("min_displacement_strength", 0.0)):
            return fires
        if _disp.get("body_ratio", 999.0) < float(params.get("min_displacement_body_ratio", 0.0)):
            return fires
        _sweep_bar = (det.get("sweep") or {}).get("bar_idx", 0)
        _choch_bar = (det.get("choch") or {}).get("bar_idx", 0)
        if (_choch_bar - _sweep_bar) > _as_int(params.get("max_sweep_age_bars", 9999)):
            return fires
        direction = str(det["direction"])
        fires.append(
            ActiveFire(
                strategy_id=int(active["id"]),
                strategy_name=strategy_name,
                strategy_family=str(active["strategy_family"]),
                strategy_version=int(active["version"]),
                symbol=symbol,
                direction=direction,
                entry=float(det["choch"]["entry_price"]),
                stop=float(det["stop_price"]),
                target=float(det["target_price"]),
                qty=qty,
                rationale=str(det.get("rationale", "judas signal")),
                features={"detector_output": det},
            )
        )
        return fires

    if engine == "buffet_zoo":
        symbol = str(active["symbol"]).upper()
        bars = bars_by_sym.get(symbol)
        if bars is None:
            return fires
        strategy_type = str(params.get("strategy_type"))
        if strategy_type == "rsi":
            result = _evaluate_rsi(bars, params)
        elif strategy_type == "ma_cross":
            result = _evaluate_ma_cross(bars, params)
        elif strategy_type == "bollinger":
            result = _evaluate_bollinger(bars, params)
        else:
            log.warning(
                "evaluate_active_strategy: buffet_zoo strategy %s has unknown strategy_type %r",
                strategy_name, strategy_type)
            return fires
        if result is None:
            return fires
        direction, entry, stop, target, features = result
        fires.append(
            ActiveFire(
                strategy_id=int(active["id"]),
                strategy_name=strategy_name,
                strategy_family=str(active["strategy_family"]),
                strategy_version=int(active["version"]),
                symbol=symbol,
                direction=direction,
                entry=entry,
                stop=stop,
                target=target,
                qty=qty,
                rationale=f"{strategy_name} fired on latest 1H bar",
                features=features,
            )
        )
        return fires

    if engine == "custom":
        # Phase 9: agent-authored strategy code dispatched through the
        # restricted runtime. Validation + sandbox happens inside
        # ``custom_strategy_runtime``; we only resolve the row and feed
        # it bars matching the active row's symbol.
        from src.research.custom_strategy_runtime import (
            evaluate_custom_strategy,
            load_custom_strategy,
        )

        try:
            custom_id = _as_int(params.get("custom_strategy_id", 0))
        except (TypeError, ValueError):
            return fires
        if custom_id <= 0:
            return fires
        import os as _os
        from pathlib import Path as _Path
        db_path = _os.environ.get(
            "JUDAS_DB_PATH",
            str(_Path(__file__).parent.parent / "judas_crew.db"),
        )
        loaded = load_custom_strategy(custom_id, db_path=db_path)
        if loaded is None:
            return fires
        code, custom_params = loaded
        symbol = str(active["symbol"]).upper()
        bars = bars_by_sym.get(symbol)
        if bars is None:
            return fires
        sig = evaluate_custom_strategy(code=code, bars=bars, params={**custom_params, "__csid__": custom_id})
        if sig is None or not isinstance(sig, dict):
            return fires
        try:
            direction = str(sig["direction"]).lower()
            entry = float(sig["entry"])
            stop = float(sig["stop"])
            target = float(sig["target"])
        except (KeyError, TypeError, ValueError):
            log.warning("custom_strategy %s returned malformed signal", custom_id)
            return fires
        if direction not in ("long", "short"):
            return fires
        feats = {"custom_strategy_id": custom_id}
        if isinstance(sig.get("features"), dict):
            feats.update(sig["features"])
        fires.append(
            ActiveFire(
                strategy_id=int(active["id"]),
                strategy_name=strategy_name,
                strategy_family=str(active["strategy_family"]),
                strategy_version=int(active["version"]),
                symbol=symbol,
                direction=direction,
                entry=entry,
                stop=stop,
                target=target,
                qty=qty,
                rationale=f"custom_strategy:{custom_id}",
                features=feats,
            )
        )
        return fires

    if engine == "buffet_pair":
        for leg in _evaluate_pair(bars_by_sym, strategy_name, params):
            fires.append(
                ActiveFire(
                    strategy_id=int(active["id"]),
                    strategy_name=strategy_name,
                    strategy_family=str(active["strategy_family"]),
                    strategy_version=int(active["version"]),
                    symbol=str(leg["symbol"]).upper(),
                    direction=str(leg["direction"]),
                    entry=float(leg["entry"]),
                    stop=float(leg["stop"]),
                    target=float(leg["target"]),
                    qty=qty,
                    rationale=f"{strategy_name} pair leg fired",
                    features=leg["features"],
                )
            )
        return fires
    return fires


async def _fetch_bars_async(symbols: set[str], host: str, port: int, client_id: int, bar_size: str = "1 hour") -> dict[str, pd.DataFrame]:
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    # NOTE: a misplaced util loop-bootstrap call used to live here. It is
    # meant for IPython / Jupyter only -- inside a real asyncio coroutine
    # we already have a running loop and bootstrapping it again patches it
    # incorrectly. Removed as part of P0b/2.
    out: dict[str, pd.DataFrame] = {}
    try:
        for symbol in sorted(symbols):
            spec = _CONTRACT_SPECS.get(symbol)
            if not spec:
                continue
            cont = ContFuture(spec["ibkr_symbol"], exchange=spec["exchange"])
            qualified = await ib.qualifyContractsAsync(cont)
            if not qualified:
                continue
            bars = await ib.reqHistoricalDataAsync(
                qualified[0],
                endDateTime="",
                durationStr="60 D",
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            )
            if not bars:
                continue
            rows = []
            for b in bars:
                ts = b.date.isoformat() if hasattr(b.date, "isoformat") else str(b.date)
                rows.append({"ts": pd.to_datetime(ts, utc=True), "open": float(b.open), "high": float(b.high), "low": float(b.low), "close": float(b.close), "volume": int(b.volume or 0)})
            out[symbol] = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    finally:
        ib.disconnect()
    return out


def fetch_bars(symbols: set[str], host: str, port: int, client_id: int, bar_size: str = "1 hour") -> dict[str, pd.DataFrame]:
    return asyncio.run(_fetch_bars_async(symbols, host, port, client_id, bar_size=bar_size))


def _build_bracket_orders(
    *,
    action: str,
    quantity: int,
    stop_price: float,
    target_price: float,
) -> tuple[MarketOrder, StopOrder, LimitOrder]:
    """Construct an explicit MKT-parent bracket.

    The previous implementation called ``ib.bracketOrder(limitPrice=0.0)``
    which builds three LMT orders and then mutated the parent into a MKT
    order in place. That left a stale ``lmtPrice=0.0`` attribute on the
    parent and depended on order-of-attribute-mutation for correctness.
    Build the three orders explicitly so each leg has the exact type and
    fields it needs, and parent the children explicitly via ``parentId``.

    The caller assigns ``parentId`` after IBKR returns an ``orderId`` for
    the parent (ib_async assigns a client-side id when the parent is
    constructed; we wire that through after ``placeOrder``). Transmit
    flags follow the IBKR bracket convention: only the LAST child set
    ``transmit=True`` so the broker activates the whole bracket atomically.
    """
    opposite = "SELL" if action == "BUY" else "BUY"
    parent = MarketOrder(action, quantity)
    parent.transmit = False
    parent.tif = "GTC"
    parent.outsideRth = True

    take_profit = LimitOrder(opposite, quantity, target_price)
    take_profit.transmit = False
    take_profit.tif = "GTC"
    take_profit.outsideRth = True

    stop_loss = StopOrder(opposite, quantity, stop_price)
    stop_loss.transmit = True
    stop_loss.tif = "GTC"
    stop_loss.outsideRth = True

    return parent, take_profit, stop_loss


async def _place_bracket_async(
    *,
    symbol: str,
    side: str,
    quantity: int,
    stop_price: float,
    target_price: float,
    host: str,
    port: int,
    client_id: int,
) -> dict[str, Any]:
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    try:
        spec = _CONTRACT_SPECS[symbol]
        cont = ContFuture(spec["ibkr_symbol"], exchange=spec["exchange"])
        q = await ib.qualifyContractsAsync(cont)
        if not q:
            raise ValueError(f"could not qualify continuous contract for {symbol}")
        month = getattr(q[0], "lastTradeDateOrContractMonth", "")
        front = Future(spec["ibkr_symbol"], lastTradeDateOrContractMonth=month, exchange=spec["exchange"])
        fq = await ib.qualifyContractsAsync(front)
        if not fq:
            raise ValueError(f"could not qualify front month for {symbol}")
        contract = fq[0]
        parent, tp, sl = _build_bracket_orders(
            action=side,
            quantity=quantity,
            stop_price=stop_price,
            target_price=target_price,
        )
        parent_t = ib.placeOrder(contract, parent)
        # Wire children to the parent now that IBKR has assigned an orderId.
        tp.parentId = parent.orderId
        sl.parentId = parent.orderId
        tp_t = ib.placeOrder(contract, tp)
        sl_t = ib.placeOrder(contract, sl)

        # Hold the connection until the bracket is confirmed by IBKR. The
        # previous implementation slept 1s and disconnected — IBKR then
        # cancelled the bracket because the client had gone away before the
        # transmit chain activated. Poll for up to 10s; only disconnect once
        # both parent and stop are at least Submitted/PreSubmitted, or the
        # poll budget is exhausted (in which case we still return so the
        # orders persist on the server even if their state was unknown).
        deadline = asyncio.get_running_loop().time() + 10.0
        confirmed_states = {"Submitted", "PreSubmitted", "Filled"}
        while True:
            parent_status = (parent_t.orderStatus.status or "")
            sl_status = (sl_t.orderStatus.status or "")
            if parent_status in confirmed_states and sl_status in confirmed_states:
                break
            if asyncio.get_running_loop().time() >= deadline:
                # Log + return; orders stay on the server.
                break
            await asyncio.sleep(0.25)
        return {
            "parent_order_id": parent.orderId,
            "tp_order_id": tp.orderId,
            "sl_order_id": sl.orderId,
            "local_symbol": contract.localSymbol,
            "status": parent_t.orderStatus.status or "Submitted",
        }
    finally:
        ib.disconnect()


def place_bracket(**kwargs) -> dict[str, Any]:
    return asyncio.run(_place_bracket_async(**kwargs))


# --- NinjaTrader execution route -----------------------------------------

_NT_BROKER_CACHE: dict[str, Any] = {}


def _resolve_nt_instrument_map(cfg_map: dict[str, str]) -> dict[str, str]:
    """Auto-roll the NT instrument map to the live ACTIVE contract.

    The config instrument_map supplies the NT product PREFIX (e.g. "MGC", "6J");
    the contract MONTH comes from the live contract bar_cache resolved this scan
    (active_contracts.json), so execution always trades the exact contract we
    signalled on. If the live month is missing/stale we OMIT the symbol rather
    than fall back to the config month — a hard-coded month silently drifts at
    every roll (MGC/ZF were already wrong), so a silent fall-through would
    re-create the bug. Omitting makes NTBroker raise -> caught upstream -> loud
    skip, no order on an unverified contract.
    """
    from src import bar_cache

    resolved: dict[str, str] = {}
    for sym, cfg_inst in cfg_map.items():
        parts = str(cfg_inst).rsplit(" ", 1)
        prefix = parts[0] if len(parts) == 2 else str(cfg_inst)
        live = bar_cache.nt_month(sym)
        if not live:
            # No fresh active contract for this config symbol. Omit it from the
            # map (NTBroker will raise -> caught upstream as a loud
            # "place_bracket failed" if a TRADED symbol actually needs it). This
            # is INFO not ERROR because most omissions are untraded config
            # symbols whose entry simply aged out (they're never fetched), which
            # is harmless — the real loud guard is at placement time.
            log.info("nt.instrument_unresolved symbol=%s — no fresh active contract "
                     "(config=%s); omitted from NT map", sym, cfg_inst)
            continue
        inst = f"{prefix} {live}"
        resolved[sym] = inst
        if inst != cfg_inst:
            log.warning("nt.instrument_autoroll symbol=%s config=%s -> active=%s", sym, cfg_inst, inst)
    return resolved


def _nt_broker():
    """Build (and cache) the NTBroker from config. Cached so each scan reuses
    one broker object; the WinRM session itself is created per command. The
    instrument_map is re-resolved on every call (cheap JSON read) so placement
    always sees the post-refresh active contracts regardless of call order."""
    from src.broker.ninjatrader import NTBroker
    from src.config import load_config

    cfg = load_config()
    nt = cfg.ninjatrader
    if nt is None:
        raise RuntimeError("execution.route=ninjatrader but config has no ninjatrader section")

    if "broker" not in _NT_BROKER_CACHE:
        _NT_BROKER_CACHE["broker"] = NTBroker(
            account=nt.account,
            instrument_map={},
            host=nt.host,
            user=nt.user,
            python_exe=nt.python_exe,
            outgoing_dir=nt.outgoing_dir,
            fill_timeout_s=nt.fill_timeout_s,
            fill_poll_s=nt.fill_poll_s,
        )
        _NT_BROKER_CACHE["cfg_map"] = dict(nt.instrument_map)

    broker = _NT_BROKER_CACHE["broker"]
    broker.instrument_map = _resolve_nt_instrument_map(_NT_BROKER_CACHE["cfg_map"])
    return broker


def place_bracket_nt(*, symbol: str, side: str, quantity: int,
                     stop_price: float, target_price: float) -> dict[str, Any] | None:
    """Place a bracket on the NT sim account. Returns a dict shaped like the
    IBKR ``place_bracket`` result (plus ``entry_fill``), or ``None`` when no
    entry fill was obtained — callers MUST treat None as "no trade placed".
    """
    broker = _nt_broker()
    spec = _CONTRACT_SPECS.get(symbol, {})
    res = broker.place_bracket(
        symbol=symbol,
        side=side,
        quantity=quantity,
        stop_price=stop_price,
        target_price=target_price,
        tick=float(spec.get("tick", 0.0)),
    )
    if res is None:
        return None
    return {
        "parent_order_id": res.entry_oid,
        "tp_order_id": res.target_oid,
        "sl_order_id": res.stop_oid,
        "local_symbol": res.instrument,
        "entry_fill": res.entry_fill_price,
        "status": "Filled",
    }


async def _cancel_order_async(*, order_id: int, host: str, port: int, client_id: int) -> str:
    """Cancel an open IBKR paper order by id. Tests monkeypatch this seam."""
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    try:
        target = None
        for trade in ib.openTrades():
            if int(trade.order.orderId) == int(order_id):
                target = trade
                break
        if target is None:
            return "not_found"
        ib.cancelOrder(target.order)
        await asyncio.sleep(0.5)
        return target.orderStatus.status or "Cancelled"
    finally:
        ib.disconnect()


def cancel_order(*, order_id: int) -> str:
    """Cancel an open paper order by id via the deterministic broker seam."""
    from src.config import load_config

    cfg = load_config()
    return asyncio.run(_cancel_order_async(
        order_id=order_id, host=cfg.ibkr.host, port=cfg.ibkr.port,
        client_id=cfg.ibkr.exec_client_id,
    ))


def _save_signal_and_trade(db_path: str, fire: ActiveFire, order: dict[str, Any] | None, decision: str) -> dict[str, Any]:
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO signals
                (ts_utc, symbol, strategy_id, strategy_family, strategy_version, direction,
                 quality_score, risk_decision, entry, stop, target, rationale, agent_notes, raw_llm_output, created_at)
            VALUES (strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            """,
            (
                fire.symbol,
                fire.strategy_id,
                fire.strategy_family,
                fire.strategy_version,
                fire.direction,
                None,
                decision,
                fire.entry,
                fire.stop,
                fire.target,
                fire.rationale,
                json.dumps(fire.features),
                json.dumps({"strategy_name": fire.strategy_name}),
            ),
        )
        signal_id = int(cur.lastrowid)
        trade_id = None
        if order and decision == "TRADE":
            # Real fill from NT when present; else fall back to signal price
            # (IBKR route doesn't return a synchronous fill price).
            entry_fill = order.get("entry_fill")
            if entry_fill is None:
                entry_fill = fire.entry
            cur = conn.execute(
                """
                INSERT INTO trades
                    (signal_id, strategy_id, strategy_family, strategy_version, ibkr_order_id,
                     tp_order_id, sl_order_id, symbol, direction, qty, entry_fill, stop_price,
                     target_price, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """,
                (
                    signal_id,
                    fire.strategy_id,
                    fire.strategy_family,
                    fire.strategy_version,
                    str(order["parent_order_id"]),
                    str(order["tp_order_id"]) if order.get("tp_order_id") is not None else None,
                    str(order["sl_order_id"]) if order.get("sl_order_id") is not None else None,
                    fire.symbol,
                    fire.direction,
                    fire.qty,
                    entry_fill,
                    fire.stop,
                    fire.target,
                ),
            )
            trade_id = int(cur.lastrowid)
    return {"signal_id": signal_id, "trade_id": trade_id}


def _reconcile_open_trades(db_path: str, bars_by_sym: dict[str, pd.DataFrame]) -> int:
    """Close any open DB trades whose stop or target was crossed by the last bar.

    This is a best-effort reconciliation: it uses the cached 1H bars to detect
    whether stop/target was hit since the trade was opened.  Fill price is set
    to the stop or target level (paper account semantics).  Returns number of
    trades closed.
    """
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        open_trades = conn.execute(
            "SELECT id, symbol, direction, qty, entry_fill, stop_price, target_price, opened_at "
            "FROM trades WHERE status = 'open'"
        ).fetchall()

    closed = 0
    for row in open_trades:
        sym = str(row["symbol"]).upper()
        bars = bars_by_sym.get(sym)
        if bars is None or bars.empty:
            continue

        opened_at = str(row["opened_at"])
        stop = float(row["stop_price"] or 0)
        target = float(row["target_price"] or 0)
        direction = str(row["direction"])
        qty = int(row["qty"] or 1)
        entry = float(row["entry_fill"] or 0)

        if not stop or not target or not entry:
            continue

        spec = _CONTRACT_SPECS.get(sym, {})
        ts_val = spec.get("tick_value", 1.0)
        ts_sz = spec.get("tick", 0.01)
        dpp = ts_val / ts_sz if ts_sz else 1.0

        # Only look at bars since the trade opened.
        try:
            since = pd.to_datetime(opened_at, utc=True)
        except Exception:
            continue
        post = bars[bars["ts"] > since]
        if post.empty:
            continue

        exit_fill: float | None = None
        exit_reason: str | None = None
        exit_ts: str | None = None

        for _, b in post.iterrows():
            low, high = float(b["low"]), float(b["high"])
            ts_str = str(b["ts"])
            if direction == "long":
                if low <= stop:
                    exit_fill = stop
                    exit_reason = "stop"
                    exit_ts = ts_str
                    break
                if high >= target:
                    exit_fill = target
                    exit_reason = "target"
                    exit_ts = ts_str
                    break
            else:
                if high >= stop:
                    exit_fill = stop
                    exit_reason = "stop"
                    exit_ts = ts_str
                    break
                if low <= target:
                    exit_fill = target
                    exit_reason = "target"
                    exit_ts = ts_str
                    break

        if exit_fill is None:
            continue

        pnl = round(
            ((exit_fill - entry) if direction == "long" else (entry - exit_fill)) * dpp * qty,
            2,
        )
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE trades SET status='closed', exit_fill=?, pnl_dollars=?, closed_at=? WHERE id=?",
                (exit_fill, pnl, exit_ts, int(row["id"])),
            )
        log.info(
            "reconcile.closed trade_id=%d sym=%s reason=%s exit=%.4f pnl=%.2f",
            int(row["id"]), sym, exit_reason, exit_fill, pnl,
        )
        closed += 1

    return closed


def _close_zombie_trade(db_path: str, trade: dict, broker=None) -> None:
    """Mark a DB-open trade closed because NT shows the position FLAT.

    Queries NT fill records for sl_order_id and tp_order_id to determine the
    real exit reason and price:
      - stop filled  → exit_reason='stop',   exit_fill=actual fill price
      - target filled → exit_reason='target', exit_fill=actual fill price
      - neither filled → exit_reason='manual_close' (user or exchange closed it)
                         — logs a WARNING so it's visible in the dashboard

    Falls back to last-bar-close price estimate only when NT fill query fails.
    """
    from src.db.models import get_conn

    sym = str(trade["symbol"]).upper()
    sl_oid = trade.get("sl_order_id") or ""
    tp_oid = trade.get("tp_order_id") or ""

    exit_px: float | None = None
    exit_reason = "reconciled_position_flat"

    # Try to get real fill info from NT for both legs.
    if broker and (sl_oid or tp_oid):
        try:
            if sl_oid:
                sl_filled, sl_price, sl_raw = broker._poll_fill(sl_oid)
                if sl_filled and sl_price:
                    exit_px = sl_price
                    exit_reason = "stop"
                    log.info("reconcile.zombie_stop_confirmed trade=%s sym=%s fill=%.4f raw=%s",
                             trade["id"], sym, sl_price, sl_raw)
            if exit_px is None and tp_oid:
                tp_filled, tp_price, tp_raw = broker._poll_fill(tp_oid)
                if tp_filled and tp_price:
                    exit_px = tp_price
                    exit_reason = "target"
                    log.info("reconcile.zombie_target_confirmed trade=%s sym=%s fill=%.4f raw=%s",
                             trade["id"], sym, tp_price, tp_raw)
            if exit_px is None:
                # Neither SL nor TP shows a fill — position closed by external action.
                exit_reason = "manual_close"
                log.warning(
                    "reconcile.manual_close_detected trade=%s sym=%s direction=%s "
                    "— position was flat on NT but neither stop nor target filled. "
                    "Likely closed manually or by session/holiday forced-liquidation. "
                    "REVIEW: was this intentional? Exit price estimated from last bar.",
                    trade["id"], sym, trade.get("direction"),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("reconcile.zombie_fill_query_failed trade=%s: %s", trade["id"], exc)

    # Fall back to last bar close for price estimate when NT query didn't resolve it.
    if exit_px is None:
        exit_px = float(trade["entry_fill"])
        for tf in ("5m", "15m", "1h"):
            try:
                exit_px = float(bar_cache.read_cache(sym, tf).iloc[-1]["close"])
                break
            except Exception:  # noqa: BLE001
                continue

    spec = _CONTRACT_SPECS.get(sym, {})
    dpp = (spec.get("tick_value", 1.0) / spec["tick"]) if spec.get("tick") else 1.0
    sign = 1 if trade["direction"] == "long" else -1
    pnl = round((exit_px - float(trade["entry_fill"])) * sign * float(trade.get("qty") or 1) * dpp, 2)
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE trades SET status='closed', exit_fill=?, pnl_dollars=?, closed_at=?, "
            "exit_reason=? WHERE id=?",
            (exit_px, pnl, _utc_now_iso(), exit_reason, trade["id"]),
        )
        conn.commit()
    log.warning("reconcile.zombie_closed trade=%s sym=%s reason=%s exit=%.4f est_pnl=%.2f",
                trade["id"], sym, exit_reason, exit_px, pnl)


def _auto_flatten_breached(db_path: str, broker, trade: dict) -> bool:
    """A position is naked AND price has already broken its stop level. Honor the
    stop's intent: market-flatten the position now (capping the loss) and close the
    DB trade with the REAL flatten price. Self-sufficient policy (Rob, Jun-18:
    'let the AI manage this') — env-gate JUDAS_AUTO_FLATTEN_NAKED=0 to fall back to
    alert-only. Returns True if the flatten was confirmed and the trade closed.

    Re-arming a breached stop is unreliable (NT rejects a sell-stop already above
    market), so a plain market flatten is the robust exit here."""
    import os
    from src.db.models import get_conn

    if os.environ.get("JUDAS_AUTO_FLATTEN_NAKED") == "0":
        return False
    sym = str(trade["symbol"]).upper()
    direction = str(trade["direction"]).lower()
    qty = int(trade.get("qty") or 1)
    try:
        flat_px = broker.flatten(sym, direction=direction, quantity=qty)
    except Exception as exc:  # noqa: BLE001
        log.critical("nt.NAKED_AUTOFLAT_ERROR trade=%s sym=%s err=%s — STILL NAKED",
                     trade["id"], sym, exc)
        return False
    if flat_px is None:
        log.critical("nt.NAKED_AUTOFLAT_UNCONFIRMED trade=%s sym=%s — STILL NAKED, manual check",
                     trade["id"], sym)
        return False
    spec = _CONTRACT_SPECS.get(sym, {})
    dpp = (spec.get("tick_value", 1.0) / spec["tick"]) if spec.get("tick") else 1.0
    sign = 1 if direction == "long" else -1
    pnl = round((float(flat_px) - float(trade["entry_fill"])) * sign * qty * dpp, 2)
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE trades SET status='closed', exit_fill=?, pnl_dollars=?, closed_at=?, "
            "exit_reason='naked_breach_autoflat' WHERE id=?",
            (float(flat_px), pnl, _utc_now_iso(), trade["id"]),
        )
        conn.commit()
    log.warning("nt.NAKED_AUTOFLATTENED trade=%s sym=%s exit=%.5f pnl=$%.2f (breached stop, auto-managed)",
                trade["id"], sym, float(flat_px), pnl)
    return True


def _try_rearm_stop(db_path: str, broker, trade: dict, instrument: str, position_qty: float) -> bool:
    """Re-place a protective OCO stop+target for a position whose stop NT cancelled
    (GTC orders die at the CME daily reset). Returns True only if a LIVE stop landed.

    Only re-arms when the stop is still protective vs the freshest price (long:
    price > stop, short: price < stop). If price already breached the stop, returns
    False so the caller auto-flattens instead (re-placing a breached stop fills at
    market anyway and NT often rejects it)."""
    import uuid as _uuid
    from src.db.models import get_conn

    sym = str(trade["symbol"]).upper()
    direction = str(trade["direction"]).lower()
    if trade.get("stop_price") is None or not instrument:
        return False
    stop_price = float(trade["stop_price"])

    price = None
    for tf in ("5m", "15m", "1h"):
        try:
            price = float(bar_cache.read_cache(sym, tf).iloc[-1]["close"])
            break
        except Exception:  # noqa: BLE001
            continue
    if price is None:
        return False
    in_range = (direction == "long" and price > stop_price) or \
               (direction == "short" and price < stop_price)
    if not in_range:
        return False  # already breached — keep the alert, let Rob decide

    qty = int(trade.get("qty") or 1)
    opp_action = "SELL" if direction == "long" else "BUY"
    oco_id = f"JC_REARM_{sym}_{_uuid.uuid4().hex[:6]}"
    try:
        stop_oid = broker._place(action=opp_action, qty=qty, order_type="STOPMARKET",
                                 limit_price=0.0, stop_price=stop_price, oco_id=oco_id,
                                 instrument=instrument)
        target_oid = ""
        if trade.get("target_price") is not None:
            target_oid = broker._place(action=opp_action, qty=qty, order_type="LIMIT",
                                       limit_price=float(trade["target_price"]), stop_price=0.0,
                                       oco_id=oco_id, instrument=instrument)
    except Exception as exc:  # noqa: BLE001
        log.critical("nt.STOP_REARM_ERROR trade=%s symbol=%s err=%s", trade["id"], sym, exc)
        return False

    if not stop_oid or str(broker.order_status(stop_oid)).lower() not in ("working", "accepted"):
        log.critical("nt.STOP_REARM_FAILED trade=%s symbol=%s stop=%.5f stop_oid=%r — STILL NAKED",
                     trade["id"], sym, stop_price, stop_oid)
        return False

    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE trades SET sl_order_id=?, tp_order_id=COALESCE(?, tp_order_id) WHERE id=?",
            (stop_oid, (target_oid or None), trade["id"]),
        )
        conn.commit()
    log.warning("nt.STOP_REARMED trade=%s symbol=%s stop=%.5f price=%.5f qty=%s new_sl=%s new_tp=%s",
                trade["id"], sym, stop_price, price, qty, stop_oid, target_oid)
    return True


def _check_position_protection(db_path: str) -> list[int]:
    """Position-aware protection check + zombie reconciler, every scan.

    For each DB-open trade, query NT for BOTH the symbol's live position and the
    stop's status in one round-trip, then:
      - stop LIVE              -> open & protected, fine.
      - stop gone, position 0  -> the trade actually CLOSED (GUID-fill reconcile
                                  missed it) -> close the zombie (no false alarm).
      - stop gone, position!=0 -> genuinely NAKED -> CRITICAL alert +
                                  outputs/naked_positions.json (the Jun-15 bug).
    Read-only on NT (never modifies orders). Returns genuinely-naked trade ids."""
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        opens = [dict(r) for r in conn.execute(
            "SELECT id, symbol, direction, qty, entry_fill, stop_price, target_price, "
            "sl_order_id, tp_order_id FROM trades WHERE status='open'"
        ).fetchall()]
    naked_file = _STATUS_DIR / "naked_positions.json"
    if not opens:
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        naked_file.write_text(json.dumps({"ts": None, "naked": []}))
        return []

    broker = _nt_broker()
    instr: dict[int, str] = {}
    for o in opens:
        try:
            instr[o["id"]] = broker._resolve_instrument(o["symbol"])
        except Exception:  # noqa: BLE001
            instr[o["id"]] = ""
    lines = []
    for o in opens:
        if instr[o["id"]]:
            lines.append(f'print("P:{o["id"]}:"+str(nt.MarketPosition("{instr[o["id"]]}","{broker.account}")))')
        if o.get("sl_order_id"):
            lines.append(f'print("S:{o["id"]}:"+str(nt.OrderStatus("{o["sl_order_id"]}")))')
    _, out = broker._nt_run(broker._HEADER + "\n".join(lines) + "\n")
    pos: dict[int, float] = {}
    stat: dict[int, str] = {}
    for line in out.splitlines():
        if line.startswith("P:"):
            _, tid, v = line.split(":", 2)
            try:
                pos[int(tid)] = float(v)
            except ValueError:
                pass
        elif line.startswith("S:"):
            _, tid, v = line.split(":", 2)
            stat[int(tid)] = v.strip()

    live = ("working", "accepted")
    naked: list[dict] = []
    rearmed: list[int] = []
    flattened: list[int] = []
    for o in opens:
        tid = o["id"]
        stop_live = str(stat.get(tid, "absent")).lower() in live
        if stop_live:
            continue
        p = pos.get(tid)
        if p == 0:
            _close_zombie_trade(db_path, o, broker=broker)  # closed on NT — reconcile, no alarm
        elif p is not None and p != 0:
            # Position open but its stop is gone — NT cancels GTC stops at the CME
            # daily reset. Self-managed (Rob, Jun-18 'let the AI manage this'):
            #   in-range  -> AUTO-RE-ARM a fresh OCO'd stop+target
            #   breached  -> AUTO-FLATTEN at market (honor the stop, cap the loss)
            # Only alerts as NAKED if BOTH the re-arm and the flatten fail.
            if _try_rearm_stop(db_path, broker, o, instr[tid], p):
                rearmed.append(tid)
            elif _auto_flatten_breached(db_path, broker, o):
                flattened.append(tid)
            else:
                naked.append(o)
                log.critical("nt.POSITION_UNPROTECTED trade=%s symbol=%s pos=%s sl_status=%s — NAKED "
                             "(re-arm + auto-flatten both failed, manual check)",
                             tid, o["symbol"], p, stat.get(tid, "absent"))
        # p is None (position query failed): don't reconcile or alarm on a bad read
    if rearmed:
        log.warning("nt.protection_rearmed count=%s trades=%s", len(rearmed), rearmed)
    if flattened:
        log.warning("nt.protection_autoflattened count=%s trades=%s", len(flattened), flattened)
    try:
        _STATUS_DIR.mkdir(parents=True, exist_ok=True)
        naked_file.write_text(json.dumps({
            "ts": _utc_now_iso(),
            "naked": [{"id": o["id"], "symbol": o["symbol"], "pos": pos.get(o["id"]),
                       "sl_status": stat.get(o["id"], "absent")} for o in naked],
        }))
    except Exception as exc:  # noqa: BLE001
        log.warning("naked_positions.json write failed: %s", exc)
    return [o["id"] for o in naked]


def _reconcile_nt_fills(db_path: str) -> int:
    """Close open trades by reading REAL NT fills from the OIF outgoing files.

    For each open trade, poll the stop and target leg GUIDs. Whichever filled
    becomes the exit (real fill price), the sibling is cancelled (OCO should
    already have done so), and pnl_dollars is computed from the actual entry
    and exit fills. Used when execution.route == "ninjatrader" instead of the
    bar-touch reconciler. Returns number of trades closed.
    """
    from src.db.models import get_conn

    broker = _nt_broker()
    with get_conn(db_path) as conn:
        open_trades = conn.execute(
            "SELECT id, symbol, direction, qty, entry_fill, sl_order_id, tp_order_id "
            "FROM trades WHERE status = 'open'"
        ).fetchall()

    closed = 0
    for row in open_trades:
        sym = str(row["symbol"]).upper()
        sl_oid = row["sl_order_id"]
        tp_oid = row["tp_order_id"]
        entry = float(row["entry_fill"] or 0)
        qty = int(row["qty"] or 1)
        direction = str(row["direction"])
        if not entry:
            continue

        exit_fill: float | None = None
        exit_reason: str | None = None
        winner_oid: str | None = None
        # Check stop leg first, then target.
        if sl_oid:
            filled, price, _raw = broker.check_fill(str(sl_oid))
            if filled:
                exit_fill, exit_reason, winner_oid = price, "stop", str(sl_oid)
        if exit_fill is None and tp_oid:
            filled, price, _raw = broker.check_fill(str(tp_oid))
            if filled:
                exit_fill, exit_reason, winner_oid = price, "target", str(tp_oid)
        if exit_fill is None:
            continue  # still open

        # Cancel the sibling leg (OCO normally handles this; belt-and-braces).
        sibling = str(tp_oid) if winner_oid == str(sl_oid) else str(sl_oid)
        if sibling and sibling != "None":
            try:
                broker.cancel(sibling, sym)
            except Exception as exc:  # noqa: BLE001
                log.warning("nt reconcile: sibling cancel failed %s: %s", sibling, exc)

        spec = _CONTRACT_SPECS.get(sym, {})
        ts_val = spec.get("tick_value", 1.0)
        ts_sz = spec.get("tick", 0.01)
        dpp = ts_val / ts_sz if ts_sz else 1.0
        pnl = round(
            ((exit_fill - entry) if direction == "long" else (entry - exit_fill)) * dpp * qty,
            2,
        )
        with get_conn(db_path) as conn:
            conn.execute(
                "UPDATE trades SET status='closed', exit_fill=?, pnl_dollars=?, exit_reason=?, "
                "closed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
                (exit_fill, pnl, exit_reason, int(row["id"])),
            )
        log.info(
            "nt_reconcile.closed trade_id=%d sym=%s reason=%s exit=%.4f pnl=%.2f",
            int(row["id"]), sym, exit_reason, exit_fill, pnl,
        )
        closed += 1

    # removed
    return closed
    return 1


def _orphan_body(db_path: str) -> int:
    return 0


def _reconcile_nt_orphans(db_path: str) -> int:
    return _orphan_body(db_path)
# body
def _gate_fire(
    db_path: str,
    fire: ActiveFire,
    *,
    max_open_positions: int,
    max_trades_per_day: int,
    skip_strategy_open_check: bool = False,
) -> str | None:
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        if not skip_strategy_open_check:
            open_for_strategy = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'open' AND strategy_id = ?",
                (fire.strategy_id,),
            ).fetchone()[0]
            if int(open_for_strategy) > 0:
                return "already_open_for_strategy"

        open_total = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'",
        ).fetchone()[0]
        if int(open_total) >= max_open_positions:
            return f"max_open_positions ({open_total}/{max_open_positions})"

        today = conn.execute(
            """
            SELECT COUNT(*)
            FROM trades
            WHERE opened_at LIKE (strftime('%Y-%m-%d','now') || '%')
            """
        ).fetchone()[0]
        if int(today) >= max_trades_per_day:
            return f"max_trades_per_day ({today}/{max_trades_per_day})"
    return None


async def _cancel_order_pair_rollback_async(*, parent_order_id: int, host: str, port: int, client_id: int) -> None:
    """Best-effort cancellation of a previously placed parent order.

    Used by the pair-atomicity rollback path. We connect with a fresh
    client and walk ``ib.openTrades()`` looking for the matching order id
    so we can call ``ib.cancelOrder`` on the live Order instance.
    """
    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=15)
    try:
        # openTrades() returns Trade objects with .order.orderId set.
        for trade in ib.openTrades():
            if int(getattr(trade.order, "orderId", -1)) == int(parent_order_id):
                ib.cancelOrder(trade.order)
        await asyncio.sleep(0.5)
    finally:
        ib.disconnect()

def _delete_signal_and_trade(db_path: str, signal_id: int | None, trade_id: int | None) -> None:
    """Roll back rows written by ``_save_signal_and_trade``.

    Used when a pair leg fails after its sibling has already been persisted.
    """
    if signal_id is None and trade_id is None:
        return
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        if trade_id is not None:
            conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        if signal_id is not None:
            conn.execute("DELETE FROM signals WHERE id = ?", (signal_id,))


def _place_fire_with_record(
    *,
    db_path: str,
    fire: ActiveFire,
    host: str,
    port: int,
    client_id: int,
    max_open_positions: int,
    max_trades_per_day: int,
    place_orders: bool,
    placed_so_far: int,
    max_new_trades: int,
    route: str = "ibkr",
    skip_strategy_open_check: bool = False,
) -> dict[str, Any]:
    """Gate, optionally place, and persist a single ActiveFire.

    Returns a dict with the decision/order/save metadata so callers can
    decide whether to roll back a sibling leg.
    """
    decision = "SKIP"
    order: dict[str, Any] | None = None
    place_error: str | None = None
    gate_reason = _gate_fire(
        db_path,
        fire,
        max_open_positions=max_open_positions,
        max_trades_per_day=max_trades_per_day,
        skip_strategy_open_check=skip_strategy_open_check,
    )
    # Auto-skip instruments NT keeps rejecting (e.g. risk-settings gap on a
    # freshly-rolled contract) until their cooldown lapses. Jun-18 self-mgmt.
    if gate_reason is None and place_orders and _is_instrument_blocked(fire.symbol):
        gate_reason = "instrument_blocked_nt_reject"
    if gate_reason is None and placed_so_far < max_new_trades and place_orders:
        side = "BUY" if fire.direction == "long" else "SELL"
        try:
            if route == "ninjatrader":
                order = place_bracket_nt(
                    symbol=fire.symbol,
                    side=side,
                    quantity=fire.qty,
                    stop_price=fire.stop,
                    target_price=fire.target,
                )
            else:
                order = place_bracket(
                    symbol=fire.symbol,
                    side=side,
                    quantity=fire.qty,
                    stop_price=fire.stop,
                    target_price=fire.target,
                    host=host,
                    port=port,
                    client_id=client_id,
                )
            # Phantom-fill guard: a None result means the entry never filled
            # (timeout / rejection / dry-run). Do NOT record an open trade at
            # signal price — that's the phantom-fill bug. Leave decision=SKIP.
            if order is None:
                fire.features["skip_reason"] = "no_fill"
                place_error = "no entry fill (None from broker)"
                if route == "ninjatrader":
                    _record_entry_rejection(fire.symbol)
            else:
                decision = "TRADE"
                if route == "ninjatrader":
                    _clear_entry_rejection(fire.symbol)
        except Exception as exc:  # noqa: BLE001 - record and surface
            place_error = str(exc)
            log.error("place_bracket failed for %s: %s", fire.symbol, exc, exc_info=True)
    elif gate_reason is None and not place_orders:
        fire.features["skip_reason"] = "eval_only_no_orders"
    elif gate_reason is not None:
        fire.features["skip_reason"] = gate_reason
    saved = _save_signal_and_trade(db_path, fire, order, decision)
    return {
        "decision": decision,
        "order": order,
        "gate_reason": gate_reason,
        "place_error": place_error,
        **saved,
    }


def _strategy_timeframe(row: dict[str, Any]) -> str:
    """Canonical timeframe for a strategy (default 1h). Detectors are
    timeframe-agnostic, so this only selects which bars the strategy is fed."""
    return bar_cache.normalize_tf(row.get("params", {}).get("timeframe"))


def _strategy_new_bar(db_path: str, strategy_id: Any, latest_bar_ts: str) -> bool:
    """New-closed-bar gate. Returns True only when ``latest_bar_ts`` is newer
    than the last bar this strategy was evaluated on, and records it.

    Without this, running the scan every few minutes would re-evaluate a
    strategy against the same (unchanged) bar over and over — re-firing the same
    setup and, once a trade closes, re-entering on the still-valid bar. It also
    means a 1h strategy is only actually evaluated when a new 1h bar appears, not
    on every 5-minute tick."""
    if strategy_id is None or not latest_bar_ts:
        return True  # can't dedup → don't block
    from src.db.models import get_conn

    with get_conn(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS strategy_bar_state ("
            "strategy_id INTEGER PRIMARY KEY, last_bar_ts TEXT)"
        )
        prev = conn.execute(
            "SELECT last_bar_ts FROM strategy_bar_state WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        if prev and prev[0] and str(latest_bar_ts) <= str(prev[0]):
            return False
        conn.execute(
            "INSERT INTO strategy_bar_state(strategy_id, last_bar_ts) VALUES(?, ?) "
            "ON CONFLICT(strategy_id) DO UPDATE SET last_bar_ts = excluded.last_bar_ts",
            (strategy_id, str(latest_bar_ts)),
        )
        conn.commit()
    return True


def run_portfolio_scan(
    *,
    db_path: str,
    host: str,
    port: int,
    data_client_id: int,
    exec_client_id: int,
    max_new_trades: int = 8,
    max_open_positions: int = 6,
    max_trades_per_day: int = 12,
    place_orders: bool = True,
    route: str = "ibkr",
) -> dict[str, Any]:
    init_db(db_path)
    # Engines the scan routes to live orders. "custom" added 2026-06-28: the
    # custom-engine branch in evaluate_active_strategy is fully implemented but was
    # never in this filter, so well-formed agent-authored ("custom") strategies
    # were promoted active yet silently never evaluated. This intentionally opens
    # the agent-code -> live (sim) path. Custom rows missing a custom_strategy_id
    # bail cleanly inside the branch (no code = no fire), so this only wakes the
    # well-formed ones.
    _LIVE_ENGINES = ("judas", "buffet", "custom")
    active = [row for row in list_active_strategies() if str(row["params"].get("execution_engine", "")).startswith(_LIVE_ENGINES)]

    def _row_symbols(row: dict[str, Any]) -> list[str]:
        params = row["params"]
        if str(params.get("execution_engine", "")) == "buffet_pair":
            seed = _find_seed_strategy(str(params.get("strategy_name")))
            return [s.upper() for s in seed.get("symbols", [])] if seed else []
        return [str(row["symbol"]).upper()]

    # Each strategy is fed bars at ITS OWN timeframe (5m/15m/1h). Collect the
    # (symbol, timeframe) pairs needed this scan and refresh them grouped by tf.
    needed_pairs: set[tuple[str, str]] = set()
    for row in active:
        tf = _strategy_timeframe(row)
        for s in _row_symbols(row):
            needed_pairs.add((s, tf))
    bars_by_pair = bar_cache.refresh_cache_pairs(needed_pairs, host=host, port=port, client_id=data_client_id)
    # 1h-only view for the ibkr-route bar-touch reconciler (NT route reconciles
    # via real fill GUIDs and ignores this).
    bars_by_sym = {s: df for (s, tf), df in bars_by_pair.items() if tf == bar_cache.DEFAULT_TF}

    # Reconcile open trades. NT route reads REAL fills from the OIF files; the
    # IBKR/legacy route uses bar-touch against the cached 1H bars.
    if route == "ninjatrader":
        try:
            _reconcile_nt_fills(db_path)
        except Exception as exc:  # noqa: BLE001
            log.error("nt fill reconcile failed: %s", exc, exc_info=True)
        try:
            _check_position_protection(db_path)
        except Exception as exc:  # noqa: BLE001
            log.error("position-protection check failed: %s", exc, exc_info=True)
    else:
        _reconcile_open_trades(db_path, bars_by_sym)

    fired: list[dict[str, Any]] = []
    placed = 0
    for row in active:
        engine = str(row["params"].get("execution_engine", ""))
        tf = _strategy_timeframe(row)
        # Feed the strategy bars at ITS timeframe (only those symbols/tf).
        tf_bars = {s: df for (s, t), df in bars_by_pair.items() if t == tf}
        # New-closed-bar gate: skip if this strategy already saw this bar (stops
        # re-firing / re-entry when the scan runs faster than the bar period).
        syms = _row_symbols(row)
        pdf = tf_bars.get(syms[0]) if syms else None
        if pdf is not None and len(pdf) and not _strategy_new_bar(db_path, row.get("id"), str(pdf.iloc[-1]["ts"])):
            continue
        fires = evaluate_active_strategy(row, tf_bars)
        is_pair = engine == "buffet_pair" and len(fires) == 2
        # Per-strategy override: eval_only_no_orders in params forces
        # place_orders=False for THIS strategy even when the scan-level flag
        # is True. Signal evaluation + recording still happen, but no orders
        # are placed (skip_reason='eval_only_no_orders' is set downstream).
        # Used by operator MCL ghost-containment to stop live fills while
        # still running the evaluation pass.
        _strategy_place_orders = place_orders and not bool(
            row["params"].get("eval_only_no_orders", False)
        )

        if not is_pair:
            for fire in fires:
                outcome = _place_fire_with_record(
                    db_path=db_path,
                    fire=fire,
                    host=host,
                    port=port,
                    client_id=exec_client_id,
                    max_open_positions=max_open_positions,
                    max_trades_per_day=max_trades_per_day,
                    place_orders=_strategy_place_orders,
                    placed_so_far=placed,
                    max_new_trades=max_new_trades,
                    route=route,
                )
                if outcome["decision"] == "TRADE":
                    placed += 1
                fired.append(
                    {
                        "strategy_name": fire.strategy_name,
                        "symbol": fire.symbol,
                        "direction": fire.direction,
                        "decision": outcome["decision"],
                        "order": outcome["order"],
                        "signal_id": outcome.get("signal_id"),
                        "trade_id": outcome.get("trade_id"),
                    }
                )
            continue

        # Pair path: place leg A, then leg B. If leg B fails to place or
        # is gated out after leg A has been transmitted, cancel leg A's
        # IBKR order AND delete leg A's signal/trade rows so we never
        # leave a single-leg orphan.
        leg_a, leg_b = fires
        outcome_a = _place_fire_with_record(
            db_path=db_path,
            fire=leg_a,
            host=host,
            port=port,
            client_id=exec_client_id,
            max_open_positions=max_open_positions,
            max_trades_per_day=max_trades_per_day,
            place_orders=_strategy_place_orders,
            placed_so_far=placed,
            max_new_trades=max_new_trades,
            route=route,
        )
        leg_a_traded = outcome_a["decision"] == "TRADE"
        if leg_a_traded:
            placed += 1

        outcome_b = _place_fire_with_record(
            db_path=db_path,
            fire=leg_b,
            host=host,
            port=port,
            client_id=exec_client_id,
            max_open_positions=max_open_positions,
            max_trades_per_day=max_trades_per_day,
            place_orders=_strategy_place_orders,
            placed_so_far=placed,
            max_new_trades=max_new_trades,
            route=route,
            # Leg A of the same pair row may have just been recorded as
            # an open trade with the same strategy_id; that's expected
            # for pairs. Bypass the per-strategy open check here.
            skip_strategy_open_check=True,
        )
        leg_b_traded = outcome_b["decision"] == "TRADE"

        # Atomicity rule: if leg A traded but leg B did NOT trade (gated,
        # errored, or place_orders disabled), unwind leg A.
        if leg_a_traded and not leg_b_traded:
            log.warning(
                "pair %s: leg A %s traded but leg B %s did not (gate=%s err=%s); rolling back leg A",
                row["params"].get("strategy_name"),
                leg_a.symbol,
                leg_b.symbol,
                outcome_b.get("gate_reason"),
                outcome_b.get("place_error"),
            )
            order_a = outcome_a["order"]
            if order_a and order_a.get("parent_order_id") is not None:
                try:
                    cancel_order(
                        parent_order_id=int(order_a["parent_order_id"]),
                        host=host,
                        port=port,
                        client_id=exec_client_id,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "leg A cancel failed for pair %s: %s",
                        row["params"].get("strategy_name"),
                        exc,
                        exc_info=True,
                    )
            _delete_signal_and_trade(
                db_path,
                outcome_a.get("signal_id"),
                outcome_a.get("trade_id"),
            )
            placed = max(0, placed - 1)
            outcome_a["decision"] = "ROLLED_BACK"
            outcome_a["order"] = None
            outcome_a["signal_id"] = None
            outcome_a["trade_id"] = None

        for fire, outcome in ((leg_a, outcome_a), (leg_b, outcome_b)):
            fired.append(
                {
                    "strategy_name": fire.strategy_name,
                    "symbol": fire.symbol,
                    "direction": fire.direction,
                    "decision": outcome["decision"],
                    "order": outcome["order"],
                    "signal_id": outcome.get("signal_id"),
                    "trade_id": outcome.get("trade_id"),
                }
            )
    return {
        "active_strategy_count": len(active),
        "needed_pairs": sorted(f"{s}@{tf}" for (s, tf) in needed_pairs),
        "bars_loaded": {f"{s}@{tf}": len(v) for (s, tf), v in bars_by_pair.items()},
        "fires": fired,
    }
