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


def test_contract_cap_is_aggregate_4():
    assert lg.contract_cap() == 4


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
    # 3 open + 1 new = 4 -> ok (not a lucid block)
    r3 = _gate_fire(db, _fire("MNQ"), max_open_positions=99, max_trades_per_day=99,
                    nt_open_contracts=3)
    assert r3 is None or not str(r3).startswith("lucid")
    # 4 open + 1 new = 5 -> blocked
    r4 = _gate_fire(db, _fire("MNQ"), max_open_positions=99, max_trades_per_day=99,
                    nt_open_contracts=4)
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
