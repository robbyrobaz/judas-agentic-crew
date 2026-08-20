"""LucidFlex 50k eval guard (2026-07-24)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.research import lucid_guard as lg

_ET = ZoneInfo("America/New_York")


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_ET).astimezone(timezone.utc)


# midday ET (no EOD window) on a Wednesday
NOON = _utc(2026, 7, 22, 12, 0)


def test_banned_symbols():
    assert lg.is_banned("MET") and lg.is_banned("mbt") and lg.is_banned("DX")
    assert not lg.is_banned("MNQ") and not lg.is_banned("MGC")


def test_contract_cap_is_aggregate_10():
    # raised 4->10 for risk-based sizing (Lucid's own limit: 4 minis/40 micros)
    assert lg.contract_cap() == 10


def test_clear_when_flat_and_midday():
    d = lg.assess(cur_equity=50_000, day_start=50_000, peak_close=50_000,
                  nt_contracts=0, now_utc=NOON)
    assert not d.halt_entries and not d.force_flat
    assert d.day_pnl == 0.0


def test_soft_profit_halts_entries_only():
    d = lg.assess(cur_equity=51_250, day_start=50_000, peak_close=50_000,
                  nt_contracts=2, now_utc=NOON)  # +$1,250 day
    assert d.halt_entries and not d.force_flat
    assert "SOFT" in d.reason


def test_hard_profit_flattens():
    d = lg.assess(cur_equity=51_600, day_start=50_000, peak_close=50_000,
                  nt_contracts=2, now_utc=NOON)  # +$1,600 day
    assert d.halt_entries and d.force_flat
    assert "HARD" in d.reason


def test_soft_loss_halts_entries_only():
    # Caps are cushion-scaled (2026-08-20). Starting cushion $2,000; a -$560 day
    # leaves $1,440 -> soft -$504 (breached), hard -$648 (not) => entries halt only.
    d = lg.assess(cur_equity=49_440, day_start=50_000, peak_close=50_000,
                  nt_contracts=1, now_utc=NOON)
    assert d.halt_entries and not d.force_flat
    assert "DAILY_LOSS_SOFT" in d.reason


def test_hard_loss_flattens():
    # -$700 day off a $2,000 cushion leaves $1,300 -> hard -$585 breached.
    # (Under the OLD flat -$900 cap this day was allowed — and five of them
    # killed LFE..104. It now flattens.)
    d = lg.assess(cur_equity=49_300, day_start=50_000, peak_close=50_000,
                  nt_contracts=2, now_utc=NOON)
    assert d.halt_entries and d.force_flat
    assert "DAILY_LOSS_HARD" in d.reason


def test_small_loss_no_halt():
    # -$300 day: cushion $1,700 -> soft -$595, untouched.
    d = lg.assess(cur_equity=49_700, day_start=50_000, peak_close=50_000,
                  nt_contracts=1, now_utc=NOON)
    assert not d.halt_entries and not d.force_flat


def test_trailing_mll_breach_flattens():
    # peak close 52,000 -> floor 50,000; equity 49,900 breaches
    d = lg.assess(cur_equity=49_900, day_start=50_500, peak_close=52_000,
                  nt_contracts=1, now_utc=NOON)
    assert d.force_flat and d.halt_entries
    assert "MLL" in d.reason
    assert d.mll_floor == 50_000.0


def test_mll_healthy_no_flatten():
    d = lg.assess(cur_equity=51_000, day_start=50_500, peak_close=52_000,
                  nt_contracts=1, now_utc=NOON)  # floor 50k, equity 51k, +$500 day
    assert not d.force_flat and not d.halt_entries


def test_eod_window_forces_flat():
    eod = _utc(2026, 7, 22, 16, 41)  # 4:41 PM ET Wed
    d = lg.assess(cur_equity=50_200, day_start=50_000, peak_close=50_000,
                  nt_contracts=3, now_utc=eod)
    assert d.force_flat and d.halt_entries
    assert "EOD" in d.reason


def test_eod_due_helper():
    assert lg.eod_flatten_due(_utc(2026, 7, 22, 16, 44))[0]     # 4:44 ET Wed
    assert not lg.eod_flatten_due(_utc(2026, 7, 22, 16, 30))[0] # 4:30 ET
    assert not lg.eod_flatten_due(_utc(2026, 7, 22, 19, 0))[0]  # 7 PM (post-reopen)
    assert not lg.eod_flatten_due(_utc(2026, 7, 25, 16, 44))[0] # Saturday


def test_ledger_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(lg, "_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(lg, "_DATA_DIR", tmp_path)
    # first close seeds start_balance
    lg.record_daily_close(50_000, now_utc=_utc(2026, 7, 20, 16, 41))  # Mon close
    lg.record_daily_close(51_000, now_utc=_utc(2026, 7, 21, 16, 41))  # Tue close
    # Wednesday: day start = Tue close, peak = max(Mon,Tue) = 51k
    assert lg.day_start_equity(now_utc=NOON) == 51_000.0
    assert lg.peak_close_equity(default=50_500, now_utc=NOON) == 51_000.0


def test_record_daily_close_rejects_zero_when_prior_nonzero(tmp_path, monkeypatch):
    """Defensive guard (2026-08-06): a bogus broker cash=$0 must NOT corrupt the
    ledger if a prior close > 0 exists. Otherwise peak_close_equity() pins to
    max(0, 0, start_balance) = start_balance → MLL floor stays at $48k → MLL
    breach becomes perpetual (the self-corrupting halt pattern from finding 2b1e5ae5)."""
    monkeypatch.setattr(lg, "_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(lg, "_DATA_DIR", tmp_path)
    # Seed legit closes
    lg.record_daily_close(49_879.70, now_utc=_utc(2026, 7, 28, 16, 41))  # Tue
    lg.record_daily_close(47_981.30, now_utc=_utc(2026, 7, 29, 16, 41))  # Wed
    peak_before = lg.peak_close_equity(default=47_981.30, now_utc=NOON)
    assert peak_before == 49_879.70
    # Simulate broker returning bogus cash=0 on next EOD flatten
    lg.record_daily_close(0.0, now_utc=_utc(2026, 7, 30, 16, 41))
    # Peak must NOT have dropped — the $0 write was rejected
    peak_after = lg.peak_close_equity(default=47_981.30, now_utc=NOON)
    assert peak_after == 49_879.70, f"ledger corrupted by bogus $0: peak={peak_after}"
    # And the 2026-07-30 entry must not exist
    led = lg._load_ledger()
    assert "2026-07-30" not in led["daily_close"]


def test_record_daily_close_rejects_zero_on_seed(tmp_path, monkeypatch):
    """First-ever call with equity=0 must NOT seed start_balance=0 (would make
    peak_close_equity = max([] + [0] + [default]) = max(default, 0), poisoning
    MLL from the start)."""
    monkeypatch.setattr(lg, "_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(lg, "_DATA_DIR", tmp_path)
    lg.record_daily_close(0.0, now_utc=_utc(2026, 7, 20, 16, 41))
    led = lg._load_ledger()
    # Should have been refused entirely — no start_balance, no closes
    assert "start_balance" not in led
    assert led.get("daily_close", {}) == {}


# --- scan-gate integration (banned / aggregate cap / halt) ------------------

def _fire(symbol, qty=1):
    from src.portfolio_runtime import ActiveFire
    return ActiveFire(strategy_id=1, strategy_name="t", strategy_family="custom",
                      strategy_version=1, symbol=symbol, direction="long",
                      entry=100.0, stop=99.0, target=102.0, qty=qty,
                      rationale="t", features={})


def test_gate_blocks_banned_symbol(tmp_path):
    from src.portfolio_runtime import _gate_fire
    from src.db.models import init_db
    db = str(tmp_path / "g.db"); init_db(db)
    assert _gate_fire(db, _fire("MET"), max_open_positions=4,
                      max_trades_per_day=24) == "lucid_banned_symbol"
    # legal symbol with room passes the lucid gates (None or a non-lucid reason)
    r = _gate_fire(db, _fire("MNQ"), max_open_positions=4, max_trades_per_day=24,
                   nt_open_contracts=0)
    assert r is None or not str(r).startswith("lucid")


def test_gate_blocks_when_halt_flag_set(tmp_path):
    from src.portfolio_runtime import _gate_fire
    from src.db.models import init_db
    db = str(tmp_path / "g.db"); init_db(db)
    assert _gate_fire(db, _fire("MNQ"), max_open_positions=4, max_trades_per_day=24,
                      lucid_block_entries=True) == "lucid_daily_guard_halt"


def test_gate_enforces_aggregate_cap(tmp_path):
    from src.portfolio_runtime import _gate_fire
    from src.db.models import init_db
    db = str(tmp_path / "g.db"); init_db(db)
    # 9 open + 1 new = 10 -> ok (not a lucid block)
    r3 = _gate_fire(db, _fire("MNQ"), max_open_positions=99, max_trades_per_day=99,
                    nt_open_contracts=9)
    assert r3 is None or not str(r3).startswith("lucid")
    # 10 open + 1 new = 11 -> blocked (cap raised 4->10 for risk-based sizing)
    r4 = _gate_fire(db, _fire("MNQ"), max_open_positions=99, max_trades_per_day=99,
                    nt_open_contracts=10)
    assert str(r4).startswith("lucid_contract_cap")


def test_lucid_guard_assess_fails_open_on_zero_cash_no_positions(monkeypatch):
    """Defensive guard (2026-08-09): if the broker returns cash=$0 with no open
    positions, treat as data unavailable (fail-open) instead of poisoning the
    MLL check. Mirrors record_daily_close_rejects_zero_when_prior_nonzero
    (write-path defense added 2026-08-06). Without this, peak_close_equity()
    pins to a stale start_balance and the MLL breach becomes perpetual,
    blocking every entry on the crew (376+ SKIPs/day seen in production).
    """
    from src.portfolio_runtime import _lucid_guard_assess

    class FakeBroker:
        def account_summary(self):
            return {"cash": 0.0, "equity": 0.0, "pnl": 0.0}
        def positions(self):
            return []

    decision, nt_contracts = _lucid_guard_assess(FakeBroker(), {})
    assert decision is None
    assert nt_contracts == 0


def test_lucid_guard_assess_does_not_fail_open_when_positions_open(monkeypatch):
    """Counter-test: if cash=$0 BUT there are open positions, the guard MUST
    still assess (the $0 cash is plausibly a real mid-session mark, not a
    broken read). Only fail-open when both are zero."""
    from src.portfolio_runtime import _lucid_guard_assess

    class FakeBroker:
        def account_summary(self):
            return {"cash": 0.0, "equity": 0.0, "pnl": 0.0}
        def positions(self):
            return [{"symbol": "MGC", "qty": 1, "side": "LONG", "avg_price": 4000.0}]

    decision, nt_contracts = _lucid_guard_assess(FakeBroker(), {})
    # Should have computed a decision (not None) because positions exist
    assert decision is not None
    assert nt_contracts == 1


def test_eval_risk_sizing():
    """Sizing at a FRESH eval cushion ($2,000 -> $100/trade budget)."""
    from src.portfolio_runtime import _eval_sized_qty
    C = 2000.0
    # MNQ ($2/pt): 15pt stop = $30 risk -> 3 lots
    assert _eval_sized_qty("MNQ", 1, 20000.0, 19985.0, cushion=C) == 3
    # MNQ wide 340pt stop = $680 -> 1 lot (the tail that kills flat sizing)
    assert _eval_sized_qty("MNQ", 1, 20000.0, 19660.0, cushion=C) == 1
    # MGC ($10/pt): 1pt stop = $10 -> would be 10, capped at 5
    assert _eval_sized_qty("MGC", 1, 4000.0, 3999.0, cushion=C) == 5
    # full-size 6J: untouched
    assert _eval_sized_qty("6J", 1, 0.0063, 0.0062, cushion=C) == 1
    # never below strategy qty, never above cap
    assert _eval_sized_qty("MNQ", 3, 20000.0, 19700.0, cushion=C) == 3


# ─── 2026-08-20 post-mortem: cushion-scaled risk ─────────────────────────────

def test_daily_caps_scale_with_cushion():
    # fresh $2k cushion -> the old fixed ceilings
    soft, hard = lg.daily_loss_caps(2000.0)
    assert soft == -700.0 and hard == -900.0
    # bled to $1,000 cushion -> caps tighten
    soft, hard = lg.daily_loss_caps(1000.0)
    assert soft == -350.0 and hard == -450.0
    # nearly dead -> allowance is bounded by the cushion itself, never above half
    soft, hard = lg.daily_loss_caps(50.0)
    assert soft == -25.0 and hard == -30.0


def test_risk_budget_scales_and_fails_small():
    assert lg.risk_budget(2000.0) == 100.0     # fresh eval
    assert lg.risk_budget(800.0) == 40.0       # bled -> min
    assert lg.risk_budget(None) == 40.0        # unknown account NEVER sizes up
    assert lg.risk_budget(99_000.0) == 250.0   # capped


def test_sizing_shrinks_as_cushion_bleeds():
    from src.portfolio_runtime import _eval_sized_qty
    # MGC $10/pt, 5pt stop = $50 risk/lot
    assert _eval_sized_qty("MGC", 1, 4000.0, 3995.0, cushion=2000.0) == 2   # $100 budget
    assert _eval_sized_qty("MGC", 1, 4000.0, 3995.0, cushion=800.0) == 1    # $40 budget
    # unknown cushion never sizes up
    assert _eval_sized_qty("MGC", 1, 4000.0, 3995.0, cushion=None) == 1
    # full-size contracts untouched
    assert _eval_sized_qty("6J", 1, 0.0063, 0.0062, cushion=2000.0) == 1


def test_replay_the_sequence_that_killed_lfe104():
    """The five real losing days of 2026-08-16..20 must NOT reach the floor.

    Actual (flat -$700/-$900 caps, flat $250/trade): -225/-496/-221/-743/-150
    = -$2,002 vs a $2,000 trail -> account dead by $1.80.
    Under cushion scaling each day's allowance shrinks, so the same *relative*
    bad run leaves the account alive."""
    cushion = 2000.0
    # each day the book loses its full soft allowance (worst realistic case)
    for _ in range(5):
        soft, _hard = lg.daily_loss_caps(cushion)
        cushion += soft          # soft is negative
        assert cushion > 0, "cushion-scaled caps must never reach the MLL floor"
    # five maximally-bad days still leave real runway
    assert cushion > 200


def test_caps_can_never_walk_through_the_floor():
    """Consecutive maximum-loss days must not breach the MLL while trading is
    permitted; below the exhausted threshold the guard stops entries."""
    cushion = 2000.0
    for _ in range(50):
        if cushion < lg.RULES["cushion_exhausted"]:
            break                # guard halts entries here — see test below
        soft, hard = lg.daily_loss_caps(cushion)
        assert hard <= soft, "hard cap must allow at least as much as soft"
        cushion += hard          # worst case: every day hits the HARD cap
        assert cushion > 0, "cushion-scaled caps walked the account through the floor"
    assert cushion > 0


def test_exhausted_cushion_halts_entries():
    # equity $48,050 vs floor $48,000 -> $50 cushion, no day loss
    d = lg.assess(cur_equity=48_050, day_start=48_050, peak_close=50_000,
                  nt_contracts=0, now_utc=NOON)
    assert d.halt_entries
    assert "CUSHION_EXHAUSTED" in d.reason
