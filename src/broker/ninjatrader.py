"""NinjaTrader order placement adapter via WinRM bridge to a Windows NT host.

Copied from judas-futures-workshop/src/broker/ninjatrader.py and adapted for
judas-agentic-crew. The crew routes EXECUTION to a NinjaTrader sim account
("SimJudasCrew") while still sourcing market DATA from IBKR (clientId 150).

Bridge pattern:
    Python here  → WinRM/PowerShell on Windows → Python on Windows →
    NinjaTrader.Client.Client() → nt.Command("PLACE", ...)

Fills come back via files written by an NT addon into:
    C:\\Users\\hartw\\Documents\\NinjaTrader 8\\outgoing\\{account}_{order_id}.txt
formatted as `FILLED;<unix_ts>;<fill_price>`.

Paper-only: the configured account is a NinjaTrader SIM account. Live
Lucid-funded accounts (LFE/LFF) are out of scope here.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import winrm  # type: ignore

log = logging.getLogger(__name__)


# --- Result shim -----------------------------------------------------------

@dataclass
class NTBracketResult:
    """Outcome of a placed bracket. GUIDs are the NT order ids (TEXT)."""
    entry_oid: str
    stop_oid: str
    target_oid: str
    instrument: str
    entry_fill_price: float
    oco_id: str


# --- Broker --------------------------------------------------------------

class NTBroker:
    """Place bracket orders on a NinjaTrader sim account via WinRM bridge.

    Bracket = MARKET entry → (block until fill) → OCO'd STOPMARKET protective
    + LIMIT take-profit. STOPMARKET + LIMIT share an `oco_id` so NT cancels
    one when the other fills.
    """

    route = "ninjatrader"

    def __init__(self, *,
                 account: str,
                 instrument_map: dict[str, str],
                 dry_run: bool = False,
                 host: str = "100.108.151.36",
                 user: str = "nqtrader",
                 password: str | None = None,
                 python_exe: str = "C:\\PyBridge\\python.exe",
                 outgoing_dir: str = r"C:\Users\hartw\Documents\NinjaTrader 8\outgoing",
                 fill_timeout_s: float = 15.0,
                 fill_poll_s: float = 1.0):
        self.account = account
        self.instrument_map = dict(instrument_map)
        self.dry_run = bool(dry_run)
        self.host = host
        self.user = user
        self.password = password or os.getenv("WINDOWS_PASSWORD", "")
        if not self.password:
            raise RuntimeError("NTBroker: WINDOWS_PASSWORD env var not set "
                               "and no password argument supplied")
        # PYTHON_EXE env var wins (matches the NQ pipeline convention), then the
        # explicit/config value, then the C:\PyBridge default.
        self.python_exe = os.getenv("PYTHON_EXE") or python_exe
        self.outgoing_dir = outgoing_dir
        self.fill_timeout_s = float(fill_timeout_s)
        self.fill_poll_s = float(fill_poll_s)

    # -- WinRM exec -----------------------------------------------------

    def _nt_run(self, script: str) -> tuple[int, str]:
        ps_cmd = f"@'\n{script}\n'@ | {self.python_exe}"
        try:
            sess = winrm.Session(self.host, auth=(self.user, self.password))
            r = sess.run_ps(ps_cmd)
            return r.status_code, r.std_out.decode()
        except Exception:
            log.exception("nt_broker.winrm_exception")
            return 1, ""

    # Execution runs on C:\PyBridge\python.exe — a standalone machine-wide
    # Python 3.10 + pythonnet living OUTSIDE any user profile. This is the
    # permanent fix for the silent outage where the exec account's WinRM logon
    # landed in a throwaway TEMP profile (TEMP.REBEL-ALLIANCE.NNN) and wiped
    # per-user `pip install --user` packages, breaking `import clr` (status=1 →
    # no trade). Because PyBridge isn't under C:\Users it's immune to that and
    # survives reboots, so `clr` loads natively — no sys.path shim needed.
    _HEADER = (
        'import sys\n'
        'sys.path.append(r"C:\\Program Files\\NinjaTrader 8\\bin")\n'
        'import clr\n'
        'clr.AddReference("NinjaTrader.Client")\n'
        'from NinjaTrader.Client import Client\n'
        'nt = Client()\n'
        'if nt.Connected(1) != 0:\n'
        '    print("CONNECTION_FAILED"); sys.exit()\n'
    )

    # -- NT primitives --------------------------------------------------

    def _resolve_instrument(self, symbol: str) -> str:
        inst = self.instrument_map.get(symbol)
        if not inst:
            raise ValueError(
                f"NTBroker has no NT instrument mapping for symbol={symbol!r}. "
                f"Add it to config.yaml ninjatrader.instrument_map."
            )
        return inst

    @staticmethod
    def _round_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return float(price)
        return round(round(price / tick) * tick, 8)

    def _place(self, *, action: str, qty: int, order_type: str,
               limit_price: float, stop_price: float, oco_id: str,
               instrument: str) -> str:
        """Issue a single nt.Command("PLACE", ...). Returns NT GUID order_id ('' on failure)."""
        script = self._HEADER + (
            'oid = nt.NewOrderId()\n'
            # GTC, not DAY: DAY stop/target orders are CANCELLED at the CME daily
            # reset (5pm CT), which silently un-protected any position held across
            # a session — they rode naked and one lost -$2,258 (MNQ, Jun 15). GTC
            # survives resets. A re-arm guard in the scan is the backstop in case
            # NT still drops one. (Entries are MARKET so TIF is moot for them.)
            f'r = nt.Command("PLACE", "{self.account}", "{instrument}", '
            f'"{action}", {qty}, "{order_type}", {limit_price:.4f}, {stop_price:.4f}, '
            f'"GTC", "{oco_id}", oid, "", "")\n'
            'print(f"PLACE_RC:{r}")\n'
            'print(f"OID:{oid}")\n'
        )
        status, out = self._nt_run(script)
        if status != 0:
            log.error("nt_broker.place_winrm_fail", extra={
                "account": self.account, "instrument": instrument,
                "action": action, "order_type": order_type, "status": status,
            })
            return ""
        rc = None
        oid = ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("PLACE_RC:"):
                try:
                    rc = int(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("OID:"):
                oid = line.split(":", 1)[1]
        if rc != 0:
            log.error("nt_broker.place_rejected", extra={
                "account": self.account, "instrument": instrument,
                "action": action, "order_type": order_type, "nt_rc": rc, "stdout": out.strip(),
            })
            return ""
        log.info("nt_broker.placed", extra={
            "account": self.account, "instrument": instrument,
            "action": action, "qty": qty, "order_type": order_type,
            "limit": limit_price, "stop": stop_price, "oco": oco_id,
            "nt_order_id": oid,
        })
        return oid

    def check_fill(self, order_id: str) -> tuple[bool, float, str]:
        """Returns (filled, fill_price, raw_status) for a single order GUID.

        Confirms the fill against TWO sources in one WinRM round-trip:
          1. The OIF outgoing file {outgoing_dir}\\{account}_{order_id}.txt
             ("FILLED;<ts>;<price>") written by the NT addon, and
          2. NT's own Client API — Filled(oid)/AvgFillPrice(oid)/OrderStatus(oid).

        A fill is only reported when at least one source confirms it. The
        reported price is NT's authoritative AvgFillPrice when available
        (it reflects real slippage), falling back to the OIF price. This is
        the "confirm the fill price" guard — never assume the signal price.
        Public so the runtime fill-sync can poll stop/target leg GUIDs.
        """
        fp = f"{self.outgoing_dir}\\{self.account}_{order_id}.txt"
        script = self._HEADER + (
            'import os\n'
            f'fp = r"{fp}"\n'
            'oif = open(fp).read().strip() if os.path.exists(fp) else "NOFILE"\n'
            'print("OIF:" + oif)\n'
            f'print("API_STATUS:" + str(nt.OrderStatus("{order_id}")))\n'
            f'print("API_FILLED:" + str(nt.Filled("{order_id}")))\n'
            f'print("API_AVGFILL:" + str(nt.AvgFillPrice("{order_id}")))\n'
        )
        status, out = self._nt_run(script)
        if status != 0:
            return False, 0.0, f"WINRM_FAIL({status})"
        oif_text = "NOFILE"
        api_status = ""
        api_filled = 0
        api_avg = 0.0
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("OIF:"):
                oif_text = line[4:]
            elif line.startswith("API_STATUS:"):
                api_status = line[len("API_STATUS:"):]
            elif line.startswith("API_FILLED:"):
                try:
                    api_filled = int(float(line[len("API_FILLED:"):]))
                except ValueError:
                    api_filled = 0
            elif line.startswith("API_AVGFILL:"):
                try:
                    api_avg = float(line[len("API_AVGFILL:"):])
                except ValueError:
                    api_avg = 0.0

        m = re.match(r"FILLED;\d+;([0-9.]+)", oif_text)
        oif_price = float(m.group(1)) if m else 0.0
        oif_filled = m is not None
        api_says_filled = api_filled > 0 and api_avg > 0
        raw = f"OIF[{oif_text}] API[status={api_status} filled={api_filled} avg={api_avg}]"

        if api_says_filled:
            # NT's own record is authoritative for the price.
            if oif_filled and abs(oif_price - api_avg) > 1e-9:
                log.warning("nt_broker.fill_price_mismatch", extra={
                    "nt_order_id": order_id, "oif_price": oif_price,
                    "api_avg_fill": api_avg,
                })
            return True, api_avg, raw
        if oif_filled:
            # OIF confirms but API hasn't caught up yet — trust the OIF price.
            return True, oif_price, raw
        if "REJECTED" in oif_text or (api_status and "Rejected" in api_status):
            return False, 0.0, raw + " REJECTED"
        return False, 0.0, raw

    def account_summary(self) -> dict[str, float] | None:
        """Query the NT sim account's cash, buying power and realized P&L.

        One WinRM round-trip. Returns None on any failure so callers can show
        a stale/blank value rather than crash.
        """
        script = self._HEADER + (
            f'print("CASH:" + str(nt.CashValue("{self.account}")))\n'
            f'print("BP:" + str(nt.BuyingPower("{self.account}")))\n'
            f'print("RPNL:" + str(nt.RealizedPnL("{self.account}")))\n'
        )
        status, out = self._nt_run(script)
        if status != 0:
            return None
        vals: dict[str, float] = {}
        keymap = {"CASH:": "cash", "BP:": "buying_power", "RPNL:": "realized_pnl"}
        for line in out.splitlines():
            line = line.strip()
            for prefix, key in keymap.items():
                if line.startswith(prefix):
                    try:
                        vals[key] = float(line[len(prefix):])
                    except ValueError:
                        pass
        if "cash" not in vals:
            return None
        return vals

    def _poll_fill(self, order_id: str) -> tuple[bool, float, str]:
        """Block up to fill_timeout_s polling outgoing file. Returns (filled, price, final_status)."""
        t0 = time.time()
        last = ""
        while time.time() - t0 < self.fill_timeout_s:
            filled, price, raw = self.check_fill(order_id)
            if raw != last:
                log.info("nt_broker.fill_poll", extra={
                    "nt_order_id": order_id, "status": raw,
                    "elapsed_s": round(time.time() - t0, 2),
                })
                last = raw
            if filled:
                return True, price, raw
            if "REJECTED" in raw:
                return False, 0.0, raw
            time.sleep(self.fill_poll_s)
        return False, 0.0, last or "TIMEOUT"

    def cancel(self, order_id: str, symbol: str) -> bool:
        if not order_id:
            return True
        instrument = self._resolve_instrument(symbol)
        script = self._HEADER + (
            f'r = nt.Command("CANCEL", "{self.account}", "{instrument}", "", 0, '
            f'"", 0.0, 0.0, "", "", "{order_id}", "", "")\n'
            'print(f"CANCEL_RC:{r}")\n'
        )
        status, out = self._nt_run(script)
        ok = status == 0 and "CANCEL_RC:0" in out
        if not ok:
            log.warning("nt_broker.cancel_failed", extra={
                "nt_order_id": order_id, "instrument": instrument,
                "winrm_status": status, "stdout": out.strip(),
            })
        return ok

    def order_status(self, order_id: str) -> str:
        """Query NT's OrderStatus for one order GUID. '' on WinRM failure.

        NT states: Working/Accepted/Submitted (live), Filled, Rejected,
        Cancelled. rc==0 from _place only means the ATI command was accepted —
        NT rejects asynchronously, so this is how we learn a stop/target was
        actually rejected (the naked-position trigger)."""
        if not order_id:
            return ""
        script = self._HEADER + f'print("STATUS:" + str(nt.OrderStatus("{order_id}")))\n'
        st, out = self._nt_run(script)
        if st != 0:
            return ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("STATUS:"):
                return line[len("STATUS:"):]
        return ""

    def _confirm_protected(self, stop_oid: str, target_oid: str) -> bool:
        """Poll both protective legs until both are live, or either is dead.

        Returns True only when BOTH legs reach a live state (Working/Accepted/
        Submitted). Returns False if either leg is missing, Rejected, or
        Cancelled, or if both don't confirm live within fill_timeout_s. NT OCO
        kills the whole pair when one leg is invalid, so one bad leg => unprotected.
        """
        if not stop_oid or not target_oid:
            return False
        live = ("working", "accepted", "submitted", "presubmitted", "filled")
        dead = ("rejected", "cancelled", "canceled", "unknown")
        t0 = time.time()
        while time.time() - t0 < self.fill_timeout_s:
            ss = self.order_status(stop_oid).lower()
            ts = self.order_status(target_oid).lower()
            if any(d in ss for d in dead) or any(d in ts for d in dead):
                log.error("nt_broker.protective_leg_dead", extra={
                    "stop_oid": stop_oid, "stop_status": ss,
                    "target_oid": target_oid, "target_status": ts,
                })
                return False
            if any(l in ss for l in live) and any(l in ts for l in live):
                return True
            time.sleep(self.fill_poll_s)
        log.error("nt_broker.protective_legs_unconfirmed", extra={
            "stop_oid": stop_oid, "target_oid": target_oid,
            "timeout_s": self.fill_timeout_s,
        })
        return False

    # -- Bracket --------------------------------------------------------

    def place_bracket(self, *, symbol: str, side: str, quantity: int,
                      stop_price: float, target_price: float,
                      tick: float = 0.0) -> Optional[NTBracketResult]:
        """Place a synthetic bracket on the NT sim account.

        Flow:
          1. PLACE entry MARKET
          2. Block up to fill_timeout_s polling outgoing for FILLED
          3. PLACE STOPMARKET protective stop (oco_id)
          4. PLACE LIMIT take-profit (same oco_id)

        Returns None on dry_run, entry fill timeout, or entry placement
        failure (caller MUST treat None as "no trade" — never record a fill).
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY/SELL, got {side!r}")
        if quantity < 1:
            raise ValueError("quantity must be >= 1")

        if tick and tick > 0:
            stop_price = self._round_to_tick(stop_price, tick)
            target_price = self._round_to_tick(target_price, tick)

        instrument = self._resolve_instrument(symbol)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        oco_id = f"JC_{symbol}_{stamp}_{uuid.uuid4().hex[:6]}"
        opp_action = "SELL" if side == "BUY" else "BUY"

        if self.dry_run:
            log.info("nt_broker.would_place_bracket", extra={
                "symbol": symbol, "instrument": instrument, "side": side,
                "qty": quantity, "stop": stop_price, "target": target_price,
                "oco_id": oco_id,
            })
            return None

        # 1) Entry MARKET
        entry_oid = self._place(
            action=side, qty=quantity, order_type="MARKET",
            limit_price=0.0, stop_price=0.0, oco_id="",
            instrument=instrument,
        )
        if not entry_oid:
            return None

        filled, entry_fill_price, status = self._poll_fill(entry_oid)
        if not filled:
            log.error("nt_broker.entry_no_fill", extra={
                "nt_order_id": entry_oid, "final_status": status,
                "symbol": symbol, "instrument": instrument,
            })
            return None

        # 2) Protective STOPMARKET (opposite side)
        stop_oid = self._place(
            action=opp_action, qty=quantity, order_type="STOPMARKET",
            limit_price=0.0, stop_price=stop_price, oco_id=oco_id,
            instrument=instrument,
        )
        # 3) Target LIMIT (same OCO)
        target_oid = self._place(
            action=opp_action, qty=quantity, order_type="LIMIT",
            limit_price=target_price, stop_price=0.0, oco_id=oco_id,
            instrument=instrument,
        )

        # NAKED-POSITION GUARD: rc==0 on _place does NOT mean NT accepted the
        # order — it rejects asynchronously. Confirm BOTH protective legs are
        # actually live. If not, the entry is open & unprotected → flatten it
        # immediately rather than hold a naked position. (Sibling repo hit this
        # live 2026-06-01.) Do NOT retry the legs — the usual cause is the
        # MARKET fill slipping past the stop level, so a re-place just re-rejects.
        if not self._confirm_protected(stop_oid, target_oid):
            log.critical("nt_broker.NAKED_RISK_flattening", extra={
                "symbol": symbol, "instrument": instrument, "side": side,
                "entry_oid": entry_oid, "stop_oid": stop_oid,
                "target_oid": target_oid, "entry_fill": entry_fill_price,
            })
            # Cancel whatever legs exist, then market-flatten the entry.
            for oid in (stop_oid, target_oid):
                if oid:
                    try:
                        self.cancel(oid, symbol)
                    except Exception:  # noqa: BLE001
                        pass
            position_dir = "long" if side == "BUY" else "short"
            flat_price = self.flatten(symbol, direction=position_dir, quantity=quantity)
            if flat_price is None:
                # Could not confirm the flatten — we may STILL be naked. This is
                # a loud abort state, not a silent no-trade.
                log.critical("nt_broker.FLATTEN_UNCONFIRMED_POSSIBLE_NAKED", extra={
                    "symbol": symbol, "instrument": instrument,
                    "position_dir": position_dir, "qty": quantity,
                    "entry_oid": entry_oid,
                    "ACTION": "MANUAL CHECK NT POSITION NOW",
                })
                raise RuntimeError(
                    f"NT bracket unprotected AND flatten unconfirmed for {symbol} "
                    f"({position_dir} {quantity}) — possible naked position, manual check"
                )
            log.warning("nt_broker.flattened_after_protection_failure", extra={
                "symbol": symbol, "entry_fill": entry_fill_price,
                "flat_price": flat_price,
            })
            return None  # entry opened then immediately closed — no live trade

        return NTBracketResult(
            entry_oid=entry_oid,
            stop_oid=stop_oid,
            target_oid=target_oid,
            instrument=instrument,
            entry_fill_price=entry_fill_price,
            oco_id=oco_id,
        )

    def flatten(self, symbol: str, *, direction: str, quantity: int) -> Optional[float]:
        """Market-flatten a position. Returns fill price or None.

        NT Client API doesn't expose direction/qty without an addon, so the
        caller MUST pass direction ('long'|'short') and quantity from the DB.
        """
        instrument = self._resolve_instrument(symbol)
        action = "SELL" if direction == "long" else "BUY"
        if self.dry_run:
            log.info("nt_broker.would_flatten", extra={
                "symbol": symbol, "instrument": instrument,
                "action": action, "qty": quantity,
            })
            return None
        oid = self._place(
            action=action, qty=int(quantity), order_type="MARKET",
            limit_price=0.0, stop_price=0.0, oco_id="",
            instrument=instrument,
        )
        if not oid:
            log.error("nt_broker.flatten_failed", extra={
                "symbol": symbol, "instrument": instrument,
                "action": action, "qty": quantity,
            })
            return None
        filled, fill_price, status = self._poll_fill(oid)
        log.info("nt_broker.flatten_result", extra={
            "symbol": symbol, "nt_order_id": oid,
            "filled": filled, "fill_price": fill_price, "status": status,
        })
        return fill_price if filled else None
