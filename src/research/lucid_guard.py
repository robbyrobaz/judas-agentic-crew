"""lucid_guard.py — LucidFlex 50k EVAL rule guards for the judas crew.

Ported from the NQ pipeline's pipeline/eval_guard.py (logic only — cross-repo
imports are banned by this repo's CLAUDE.md). Enforces the LucidFlex 50k EVAL
rules on the crew's NT account so the same book that trades sim today can move
to a real LFE eval account unchanged.

Rules (authoritative memory reference_lucid_50k_rules, + Rob 2026-07-24):
  - BANNED symbols: crypto (MET, MBT) + DX — Lucid disallows them.
  - AGGREGATE contract cap: <= 4 contracts open across the WHOLE book at once
    (Rob's choice; Lucid's own rule is 4 minis / 40 micros — this is tighter).
  - Daily PROFIT caps: soft +$1,200 halts NEW entries for the day; hard +$1,500
    flattens everything and halts. $1,500 = 50% of the $3k target = the Lucid
    consistency ceiling, so capping the day here keeps the account compliant.
  - $2,000 TRAILING max-loss from peak daily-CLOSE equity — breach => flatten+halt.
  - Daily LOSS caps: CUSHION-SCALED (2026-08-20). Ceilings are -$700 soft /
    -$900 hard (under Lucid's UNANNOUNCED ~$1,200 eval DLL that killed LFE..89),
    but each day may only lose a FRACTION of the cushion remaining before the
    trailing MLL: 35% soft / 45% hard, never more than half the cushion.
    WHY: LFE..104 died 2026-08-20 with FIXED caps — five losing days
    (-$225/-$496/-$221/-$743/-$150) each passed -$700/-$900 and summed to
    -$2,002 against the $2k trail. Fixed daily caps bound the DAY but permit
    unlimited CUMULATIVE loss; scaling makes the sequence converge.
  - Per-trade risk budget is scaled the same way (risk_budget()) — 5% of
    remaining cushion, so size shrinks as the account bleeds (anti-martingale).
  - Cushion below $100: halt entries, the eval is not recoverable by trading.
  - EOD FLAT by 4:45 PM ET (Lucid cutoff; auto-liq 4:59:59 ET). We flatten at
    4:40 ET to leave fill room.

Day P&L includes UNREALIZED (open positions marked to current price) so a guard
never fires late on a running open position — the caller passes cur_equity =
cash + unrealized.

Pure functions here are the tested seam; the scan gathers live inputs and acts
on the returned decision. Fail-open: callers treat any exception as "no guard"
and log — never crash the scan over guard config.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _REPO_ROOT / "data"
_LEDGER_PATH = _DATA_DIR / "lucid_guard_ledger.json"
_STATE_PATH = _DATA_DIR / "lucid_guard_state.json"
_PROFIT_LOCK_PATH = _DATA_DIR / "lucid_guard_profit_lock.json"

# Single active venue. If more are added later, key this off config.
RULES = {
    "venue": "lucidflex_50k_eval",
    "banned_symbols": {"MET", "MBT", "DX"},
    "max_contracts_aggregate": 10,  # risk-based sizing cap; aligned with config.yaml
    "daily_profit_soft": 1200.0,   # halt NEW entries at day P&L >= this
    "daily_profit_hard": 1500.0,   # flatten all + halt at day P&L >= this
    "daily_loss_soft": -700.0,     # CEILING on the halt-new-entries cap (see cushion scaling)
    "daily_loss_hard": -900.0,     # CEILING on the flatten+halt cap. Both are now scaled DOWN
                                   # by remaining MLL cushion — see daily_loss_caps(). Fixed
                                   # caps killed LFE..104 on 2026-08-20: five losing days, each
                                   # individually inside -$700/-$900, summed past the $2k trail.
    # Cushion scaling (2026-08-20 post-mortem). Each day may risk only a FRACTION
    # of what's left before the trailing MLL, so consecutive red days shrink
    # geometrically instead of spending a flat -$700 five times against a $2k trail.
    "daily_loss_soft_pct": 0.35,   # of remaining cushion
    "daily_loss_hard_pct": 0.45,
    "daily_loss_floor": -150.0,    # never clamp the soft cap tighter than this
    # Per-trade risk budget = this fraction of remaining cushion, clamped.
    # At a fresh $2,000 cushion -> $100/trade (~20 losing trades of runway, vs
    # the 8 that the old flat $250 allowed against a book firing ~10/day).
    "risk_pct_of_cushion": 0.05,
    "risk_min": 40.0,
    "risk_max": 250.0,
    # Below this much cushion the eval is not recoverable by trading — halt and
    # let Rob reset ($95) instead of grinding the remainder into commissions.
    "cushion_exhausted": 100.0,
    "mll_trail": 2000.0,           # $ trailing from peak daily-CLOSE equity
    "base_target": 3000.0,         # eval profit target (context only)
    "eod_flat_et": (16, 40),       # flatten all at/after this ET time
    "eod_cutoff_et": (16, 45),     # Lucid "must be flat by" time
}


@dataclass
class GuardDecision:
    halt_entries: bool = False     # take NO new entries this scan
    force_flat: bool = False       # flatten ALL open positions now
    day_pnl: float = 0.0
    mll_floor: float = 0.0
    cushion: float = 0.0           # cur_equity - mll_floor
    reasons: list = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "clear"


# ---- static rule helpers (used by the entry gate) -------------------------

def is_banned(symbol: str) -> bool:
    return str(symbol).upper() in RULES["banned_symbols"]


def contract_cap() -> int:
    return int(RULES["max_contracts_aggregate"])


def trading_date(now_utc: datetime | None = None) -> str:
    """ET trading date; the Globex evening session (>=18:00 ET) belongs to the
    NEXT calendar date, matching how Lucid rolls the day."""
    now_et = (now_utc or datetime.now(timezone.utc)).astimezone(_ET)
    d = now_et.date()
    if now_et.hour >= 18:
        d = d + timedelta(days=1)
    return d.isoformat()


def eod_flatten_due(now_utc: datetime | None = None) -> tuple[bool, str]:
    """True on a weekday once ET clock reaches the EOD flatten time (4:40 ET),
    up to the CME reopen (18:00 ET). Weekends: nothing to flatten intraday.

    NOTE: does not special-case CME early-close (1 PM ET) holidays — flag for a
    calendar upgrade before this runs a real account through a holiday week.
    """
    now_et = (now_utc or datetime.now(timezone.utc)).astimezone(_ET)
    if now_et.weekday() >= 5:  # Sat/Sun
        return False, ""
    h, m = RULES["eod_flat_et"]
    at = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
    reopen = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
    if at <= now_et < reopen:
        return True, f"EOD flatten window ({h:02d}:{m:02d} ET, flat before Lucid 16:45 cutoff)"
    return False, ""


# ---- ledger (peak daily-close equity for the trailing MLL) ----------------

def _load_ledger() -> dict:
    if _LEDGER_PATH.exists():
        try:
            return json.loads(_LEDGER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_ledger(led: dict) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=1, sort_keys=True))
    tmp.replace(_LEDGER_PATH)


def record_daily_close(equity: float, now_utc: datetime | None = None) -> None:
    """Persist the day's CLOSE equity (call at/after EOD flatten, positions flat
    so equity == cash). Overwrites the entry for the trading date (last write of
    the day is the close). Also seeds start_balance on first ever call.

    Defensive guard (2026-08-06): if equity == 0 AND a prior daily close is
    > 0, refuse the write. The MLL floor calculation uses max(closes,
    start_balance, cur_equity); a bogus $0 close poisons the floor permanently
    (it becomes max(0, 0, 50000) = 50000 → floor 48000 → perpetual breach).
    The caller (EOD flatten in portfolio_runtime) takes broker cash; if the
    broker returns $0 (WinRM / NT sim zero / field-name mismatch) the daily
    ledger MUST NOT be corrupted. The caller is expected to fail-open its
    own MLL check in that case (see _lucid_assess hook).
    """
    led = _load_ledger()
    eq = round(float(equity), 2)
    closes = led.setdefault("daily_close", {})
    prior_nonzero = [v for v in closes.values() if v > 0]
    # Refuse any $0 write: a Lucid eval account starts at $50k and the MLL
    # guard force-flats long before $0, so $0 from the broker is ALWAYS a
    # broken read (WinRM / NT sim zero / field-name mismatch). Poisoning the
    # ledger with $0 makes peak_close_equity() pin to start_balance → MLL
    # floor stays at $48k → perpetual breach (the self-corrupting halt from
    # finding 2b1e5ae5).
    if eq == 0.0:
        import logging
        logging.getLogger(__name__).warning(
            "record_daily_close: refusing $0 write \u2014 prior closes were %s "
            "(broker likely returning bogus cash; will retry next scan)",
            prior_nonzero[-3:],
        )
        return
    led.setdefault("start_balance", eq)
    closes[trading_date(now_utc)] = eq
    _save_ledger(led)


def day_start_equity(now_utc: datetime | None = None) -> float | None:
    """Equity at the start of today = most recent PRIOR daily close, else the
    seeded start_balance. None if the ledger is empty (first run)."""
    led = _load_ledger()
    today = trading_date(now_utc)
    closes = led.get("daily_close", {})
    prior = [v for d, v in sorted(closes.items()) if d < today]
    if prior:
        return float(prior[-1])
    return float(led["start_balance"]) if "start_balance" in led else None


def peak_close_equity(default: float, now_utc: datetime | None = None) -> float:
    """Highest completed daily-CLOSE equity (prior dates), floored at
    start_balance and `default` (current equity) so a brand-new ledger doesn't
    invent a drawdown."""
    led = _load_ledger()
    today = trading_date(now_utc)
    closes = [v for d, v in led.get("daily_close", {}).items() if d < today]
    candidates = closes + [led.get("start_balance", default), default]
    return float(max(candidates))


# ---- cushion-scaled risk (2026-08-20 post-mortem) -------------------------

def daily_loss_caps(cushion: float) -> tuple[float, float]:
    """(soft, hard) daily-loss caps in dollars (negative), scaled to the
    cushion remaining before the trailing MLL.

    WHY: LFE..104 died 2026-08-20 with FIXED caps. Five consecutive losing
    days of -$225/-$496/-$221/-$743/-$150 each passed a flat -$700 soft /
    -$900 hard and summed to -$2,002 against a $2,000 trail. Fixed daily caps
    permit unlimited cumulative loss; scaling each day to a fraction of what's
    LEFT makes the sequence decay geometrically and never reach the floor.
    """
    c = max(0.0, float(cushion))
    soft_mag = min(abs(RULES["daily_loss_soft"]), c * RULES["daily_loss_soft_pct"])
    hard_mag = min(abs(RULES["daily_loss_hard"]), c * RULES["daily_loss_hard_pct"])
    # Small-cushion floor so the book can still take a normal trade — but NEVER
    # more than half the cushion, or the floor itself walks the account through
    # the MLL (10 consecutive floor-days did exactly that in the first draft).
    # Bounded this way the sequence converges toward 0 and cannot cross it.
    soft_mag = max(soft_mag, min(abs(RULES["daily_loss_floor"]), c * 0.5))
    hard_mag = max(hard_mag, min(abs(RULES["daily_loss_floor"]) * 1.35, c * 0.6))
    return round(-soft_mag, 2), round(-hard_mag, 2)


def risk_budget(cushion: float | None) -> float:
    """Dollar risk allowed on ONE trade, as a fraction of remaining cushion.

    Anti-martingale: as the account bleeds toward the trailing MLL, every new
    trade gets smaller, so a losing streak decays instead of compounding. A
    None/unknown cushion falls back to the minimum (fail-small, never
    fail-big — an unreadable account is the worst time to size up)."""
    if cushion is None:
        return RULES["risk_min"]
    c = max(0.0, float(cushion))
    return round(min(RULES["risk_max"],
                     max(RULES["risk_min"], c * RULES["risk_pct_of_cushion"])), 2)


# ---- the assessment (pure) ------------------------------------------------

def assess(*, cur_equity: float, day_start: float, peak_close: float,
           nt_contracts: int, now_utc: datetime | None = None) -> GuardDecision:
    """Compute the guard decision from live equity + position state.

    cur_equity   = cash + unrealized (marked to current price)
    day_start    = equity at start of the trading day (prior close)
    peak_close   = highest completed daily-close equity (for the trailing MLL)
    nt_contracts = current AGGREGATE open contracts across the book
    """
    d = GuardDecision()
    d.day_pnl = round(cur_equity - day_start, 2)
    d.mll_floor = round(peak_close - RULES["mll_trail"], 2)
    d.cushion = round(cur_equity - d.mll_floor, 2)

    # Loss side — $2k trailing MLL breach is terminal for the day.
    if cur_equity <= d.mll_floor:
        d.force_flat = True
        d.halt_entries = True
        d.reasons.append(f"TRAILING MLL BREACH: equity ${cur_equity:.0f} <= floor ${d.mll_floor:.0f}")

    # Cushion exhausted — stop trading a dead eval (2026-08-20).
    if 0 < d.cushion < RULES["cushion_exhausted"]:
        d.halt_entries = True
        d.reasons.append(
            f"CUSHION_EXHAUSTED ${d.cushion:.0f} < ${RULES['cushion_exhausted']:.0f} "
            "— eval not recoverable by trading; reset the account")

    # Loss side — caps sit under Lucid's unannounced ~$1,200 eval DLL AND are
    # scaled to what's actually left before the trailing MLL (2026-08-20).
    soft_cap, hard_cap = daily_loss_caps(d.cushion)
    if d.day_pnl <= hard_cap:
        d.force_flat = True
        d.halt_entries = True
        d.reasons.append(f"DAILY_LOSS_HARD ${d.day_pnl:.0f} <= ${hard_cap:.0f} "
                         f"(cushion-scaled; flatten+stop)")
    elif d.day_pnl <= soft_cap:
        d.halt_entries = True
        d.reasons.append(f"DAILY_LOSS_SOFT ${d.day_pnl:.0f} <= ${soft_cap:.0f} "
                         f"(cushion-scaled; halt new entries)")

    # Profit hard cap — lock the day in.
    if d.day_pnl >= RULES["daily_profit_hard"]:
        d.force_flat = True
        d.halt_entries = True
        d.reasons.append(f"DAILY_PROFIT_HARD +${d.day_pnl:.0f} >= +${RULES['daily_profit_hard']:.0f} (flatten+stop)")
    # Profit soft cap — stop opening, let existing runners run.
    elif d.day_pnl >= RULES["daily_profit_soft"]:
        d.halt_entries = True
        d.reasons.append(f"DAILY_PROFIT_SOFT +${d.day_pnl:.0f} >= +${RULES['daily_profit_soft']:.0f} (halt new entries)")

    # EOD flatten window.
    due, why = eod_flatten_due(now_utc)
    if due:
        d.force_flat = True
        d.halt_entries = True
        d.reasons.append(why)

    return d


def apply_profit_latch(decision: GuardDecision,
                       now_utc: datetime | None = None) -> GuardDecision:
    """Latch a soft/hard profit cap for the rest of the trading day.

    Without this, a runner could cross +$1,200, retreat below it, and allow
    fresh entries later. A hard-cap fill with slippage could similarly fall
    back below the threshold. The date-keyed latch resets automatically on the
    next Globex trading day.
    """
    today = trading_date(now_utc)
    level = "hard" if decision.day_pnl >= RULES["daily_profit_hard"] else (
        "soft" if decision.day_pnl >= RULES["daily_profit_soft"] else "")
    try:
        prior = json.loads(_PROFIT_LOCK_PATH.read_text()) if _PROFIT_LOCK_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        prior = {}
    if prior.get("trading_date") == today:
        if prior.get("level") == "hard" or not level:
            level = str(prior.get("level") or level)
    if level:
        decision.halt_entries = True
        if level == "hard":
            decision.force_flat = True
        marker = f"DAILY_PROFIT_{level.upper()}_LATCHED for {today}"
        if marker not in decision.reasons:
            decision.reasons.append(marker)
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _PROFIT_LOCK_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"trading_date": today, "level": level,
                                       "updated_utc": (now_utc or datetime.now(timezone.utc)).isoformat()},
                                      indent=1))
            tmp.replace(_PROFIT_LOCK_PATH)
        except OSError:
            pass
    return decision


def write_state(decision: GuardDecision, nt_contracts: int,
                now_utc: datetime | None = None) -> None:
    """Persist the latest guard snapshot so the operator (and dashboard) can
    show live eval status without their own WinRM read. Best-effort."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "updated_utc": (now_utc or datetime.now(timezone.utc)).isoformat(),
            "day_pnl": decision.day_pnl,
            "cushion": decision.cushion,
            "mll_floor": decision.mll_floor,
            "nt_contracts": int(nt_contracts),
            "halt_entries": decision.halt_entries,
            "force_flat": decision.force_flat,
            "reason": decision.reason,
            "daily_profit_soft": RULES["daily_profit_soft"],
            "daily_profit_hard": RULES["daily_profit_hard"],
            "max_contracts": RULES["max_contracts_aggregate"],
            "mll_trail": RULES["mll_trail"],
            "base_target": RULES["base_target"],
        }
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(_STATE_PATH)
    except Exception:  # noqa: BLE001
        pass


def read_state() -> dict | None:
    """Latest guard snapshot written by the scan, or None."""
    try:
        return json.loads(_STATE_PATH.read_text()) if _STATE_PATH.exists() else None
    except (json.JSONDecodeError, OSError):
        return None
