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

from src.db.models import init_db
from src.strategy_registry import list_active_strategies
from src.tools.judas_detector import run_judas_detection_rich

log = logging.getLogger(__name__)


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
    period = int(params["period"])
    lo = float(params["lo_thr"])
    hi = float(params["hi_thr"])
    target_r = float(params["target_r"])
    sa = float(params["stop_atr_mult"])
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
    fast = int(params["fast"])
    slow = int(params["slow"])
    target_r = float(params["target_r"])
    sa = float(params["stop_atr_mult"])
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
    period = int(params["period"])
    n_std = float(params["n_std"])
    target_r = float(params["target_r"])
    sa = float(params["stop_atr_mult"])
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
    window = int(params["window"])
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
    qty = int(params.get("qty", 1))

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
            bars_df=bars.tail(int(params.get("detector_lookback_bars", 120))).copy(),
            prior_high=prior_high,
            prior_low=prior_low,
            tick_size=float(spec["tick"]),
            confirmation_bars=int(params.get("confirmation_bars", 4)),
            pivot_length=int(params.get("pivot_length", 2)),
            target_r=float(params.get("target_r", 2.0)),
            stop_buffer_ticks=int(params.get("stop_buffer_ticks", 2)),
            min_sweep_ticks=int(params.get("min_sweep_ticks", 3)),
            dollar_per_point=float(spec["tick_value"] / spec["tick"]),
        )
        if not det.get("pattern_found"):
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
            custom_id = int(params.get("custom_strategy_id", 0))
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
        sig = evaluate_custom_strategy(code=code, bars=bars, params=custom_params)
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


async def _fetch_bars_async(symbols: set[str], host: str, port: int, client_id: int) -> dict[str, pd.DataFrame]:
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
                barSizeSetting="1 hour",
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


def fetch_bars(symbols: set[str], host: str, port: int, client_id: int) -> dict[str, pd.DataFrame]:
    return asyncio.run(_fetch_bars_async(symbols, host, port, client_id))


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
            cur = conn.execute(
                """
                INSERT INTO trades
                    (signal_id, strategy_id, strategy_family, strategy_version, ibkr_order_id,
                     symbol, direction, qty, entry_fill, stop_price, target_price, status, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """,
                (
                    signal_id,
                    fire.strategy_id,
                    fire.strategy_family,
                    fire.strategy_version,
                    str(order["parent_order_id"]),
                    fire.symbol,
                    fire.direction,
                    fire.qty,
                    fire.entry,
                    fire.stop,
                    fire.target,
                ),
            )
            trade_id = int(cur.lastrowid)
    return {"signal_id": signal_id, "trade_id": trade_id}


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


def cancel_order(*, parent_order_id: int, host: str, port: int, client_id: int) -> None:
    asyncio.run(_cancel_order_async(
        parent_order_id=parent_order_id, host=host, port=port, client_id=client_id,
    ))


def _delete_signal_and_trade(db_path: str, signal_id: int | None, trade_id: int | None) -> None:
    """Roll back rows written by ``_save_signal_and_trade``.

    Used when a pair leg fails after its sibling has already been
    persisted -- we must not leave an orphan single-leg pair in the DB.
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
    if gate_reason is None and placed_so_far < max_new_trades and place_orders:
        side = "BUY" if fire.direction == "long" else "SELL"
        try:
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
            decision = "TRADE"
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
) -> dict[str, Any]:
    init_db(db_path)
    active = [row for row in list_active_strategies() if str(row["params"].get("execution_engine", "")).startswith(("judas", "buffet"))]
    needed_symbols: set[str] = set()
    for row in active:
        params = row["params"]
        engine = str(params.get("execution_engine", ""))
        if engine == "buffet_pair":
            seed = _find_seed_strategy(str(params.get("strategy_name")))
            if seed:
                needed_symbols.update(seed.get("symbols", []))
        else:
            needed_symbols.add(str(row["symbol"]).upper())
    bars_by_sym = fetch_bars(needed_symbols, host=host, port=port, client_id=data_client_id)

    fired: list[dict[str, Any]] = []
    placed = 0
    for row in active:
        engine = str(row["params"].get("execution_engine", ""))
        fires = evaluate_active_strategy(row, bars_by_sym)
        is_pair = engine == "buffet_pair" and len(fires) == 2

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
                    place_orders=place_orders,
                    placed_so_far=placed,
                    max_new_trades=max_new_trades,
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
            place_orders=place_orders,
            placed_so_far=placed,
            max_new_trades=max_new_trades,
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
            place_orders=place_orders,
            placed_so_far=placed,
            max_new_trades=max_new_trades,
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
        "needed_symbols": sorted(needed_symbols),
        "bars_loaded": {k: len(v) for k, v in bars_by_sym.items()},
        "fires": fired,
    }
