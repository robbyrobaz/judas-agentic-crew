"""Session and market-status tools.

Deterministic guard so the crew can skip weekends and out-of-window runs
without relying on LLM inference from timestamps alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from crewai.tools import tool

from src.config import load_config


@dataclass
class SessionWindow:
    name: str
    start_hhmm: str
    end_hhmm: str


def _load_session_windows() -> list[SessionWindow]:
    windows = []
    import yaml

    cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
    with open(cfg_path) as f:
        raw_yaml = yaml.safe_load(f)
    sessions = raw_yaml.get("schedule", {}).get("active_sessions", [])
    for item in sessions:
        windows.append(
            SessionWindow(
                name=item["name"],
                start_hhmm=item["start"],
                end_hhmm=item["end"],
            )
        )
    return windows


def _load_schedule_meta() -> dict[str, str]:
    import yaml

    cfg_path = Path(__file__).parent.parent.parent / "config.yaml"
    with open(cfg_path) as f:
        raw_yaml = yaml.safe_load(f)
    schedule = raw_yaml.get("schedule", {})
    return {
        "hard_flat_deadline": schedule.get("hard_flat_deadline", "16:45"),
        "daily_reset": schedule.get("daily_reset", "17:00"),
    }


def _in_window(now_et: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    start_h, start_m = [int(x) for x in start_hhmm.split(":")]
    end_h, end_m = [int(x) for x in end_hhmm.split(":")]
    start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now_et <= end


def _et_anchor(now_et: datetime, hhmm: str) -> datetime:
    h, m = [int(x) for x in hhmm.split(":")]
    return now_et.replace(hour=h, minute=m, second=0, microsecond=0)


def _globex_open_now(now_et: datetime) -> tuple[bool, str]:
    weekday = now_et.weekday()
    hhmm = now_et.hour * 60 + now_et.minute
    daily_break_start = 17 * 60
    daily_break_end = 18 * 60

    if weekday == 5:
        return False, "Saturday: Globex closed."
    if weekday == 6:
        if hhmm < daily_break_end:
            return False, "Sunday before 6:00 p.m. ET: Globex closed."
        return True, "Sunday evening Globex session is open."
    if weekday == 4 and hhmm >= daily_break_start:
        return False, "Friday after 5:00 p.m. ET: weekly futures close."
    if weekday in (0, 1, 2, 3) and daily_break_start <= hhmm < daily_break_end:
        return False, "Daily maintenance break: 5:00–6:00 p.m. ET."
    return True, "Globex is open."


@tool("session_status_tool")
def session_status_tool(input_json: str = "{}") -> str:
    """Return whether the current time is inside an allowed trade session.

    No input required.

    Returns JSON:
    {
      "can_trade_now": bool,
      "market_open_now": bool,
      "strategy_window_open": bool,
      "is_weekend": bool,
      "active_session": "ny" | "london" | null,
      "near_daily_close": bool,
      "flatten_positions_now": bool,
      "daily_close_et": "17:00",
      "now_utc": str,
      "now_et": str,
      "reason": str
    }
    """
    _ = input_json
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    now_phx = now_utc.astimezone(ZoneInfo("America/Phoenix"))
    is_weekend = now_et.weekday() >= 5
    market_open_now, market_reason = _globex_open_now(now_et)
    schedule_meta = _load_schedule_meta()
    hard_flat_deadline = schedule_meta["hard_flat_deadline"]
    daily_reset = schedule_meta["daily_reset"]
    flatten_minutes = load_config().risk.flatten_before_daily_close_minutes
    close_anchor = _et_anchor(now_et, daily_reset)
    hard_flat_anchor = _et_anchor(now_et, hard_flat_deadline)
    near_daily_close = (close_anchor - timedelta(minutes=flatten_minutes)) <= now_et < close_anchor
    flatten_positions_now = hard_flat_anchor <= now_et < close_anchor and market_open_now

    active_session = None
    strategy_window_open = False
    if market_open_now:
        for window in _load_session_windows():
            if _in_window(now_et, window.start_hhmm, window.end_hhmm):
                active_session = window.name
                strategy_window_open = True
                break

    can_trade_now = market_open_now and strategy_window_open and not near_daily_close

    if not market_open_now:
        return json.dumps(
            {
                "can_trade_now": False,
                "market_open_now": False,
                "strategy_window_open": strategy_window_open,
                "is_weekend": is_weekend,
                "active_session": active_session,
                "near_daily_close": near_daily_close,
                "flatten_positions_now": flatten_positions_now,
                "daily_close_et": daily_reset,
                "hard_flat_deadline_et": hard_flat_deadline,
                "now_utc": now_utc.isoformat(),
                "now_et": now_et.isoformat(),
                "now_phx": now_phx.isoformat(),
                "reason": market_reason,
            }
        )

    if near_daily_close:
        return json.dumps(
            {
                "can_trade_now": False,
                "market_open_now": True,
                "strategy_window_open": strategy_window_open,
                "is_weekend": is_weekend,
                "active_session": active_session,
                "near_daily_close": True,
                "flatten_positions_now": True,
                "daily_close_et": daily_reset,
                "hard_flat_deadline_et": hard_flat_deadline,
                "now_utc": now_utc.isoformat(),
                "now_et": now_et.isoformat(),
                "now_phx": now_phx.isoformat(),
                "reason": (
                    f"Near daily futures close: flatten positions by {hard_flat_deadline} ET "
                    f"and do not open new trades."
                ),
            }
        )

    if not strategy_window_open:
        return json.dumps(
            {
                "can_trade_now": False,
                "market_open_now": True,
                "strategy_window_open": False,
                "is_weekend": is_weekend,
                "active_session": None,
                "near_daily_close": False,
                "flatten_positions_now": False,
                "daily_close_et": daily_reset,
                "hard_flat_deadline_et": hard_flat_deadline,
                "now_utc": now_utc.isoformat(),
                "now_et": now_et.isoformat(),
                "now_phx": now_phx.isoformat(),
                "reason": "Globex is open, but this repo is outside its configured London/NY entry windows.",
            }
        )

    return json.dumps(
        {
            "can_trade_now": True,
            "market_open_now": True,
            "strategy_window_open": True,
            "is_weekend": is_weekend,
            "active_session": active_session,
            "near_daily_close": False,
            "flatten_positions_now": False,
            "daily_close_et": daily_reset,
            "hard_flat_deadline_et": hard_flat_deadline,
            "now_utc": now_utc.isoformat(),
            "now_et": now_et.isoformat(),
            "now_phx": now_phx.isoformat(),
            "reason": (
                f"Globex is open and the strategy is inside the configured {active_session} session window."
            ),
        }
    )
