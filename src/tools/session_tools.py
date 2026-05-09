"""Session and market-status tools.

Deterministic guard so the crew can skip weekends and out-of-window runs
without relying on LLM inference from timestamps alone.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from crewai.tools import tool


@dataclass
class SessionWindow:
    name: str
    start_hhmm: str
    end_hhmm: str


def _load_session_windows() -> list[SessionWindow]:
    windows = []
    # config.yaml comments indicate active_sessions are expressed in ET
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


def _in_window(now_et: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    start_h, start_m = [int(x) for x in start_hhmm.split(":")]
    end_h, end_m = [int(x) for x in end_hhmm.split(":")]
    start = now_et.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now_et.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return start <= now_et <= end


@tool("session_status_tool")
def session_status_tool(input_json: str = "{}") -> str:
    """Return whether the current time is inside an allowed trade session.

    No input required.

    Returns JSON:
    {
      "can_trade_now": bool,
      "is_weekend": bool,
      "active_session": "ny" | "london" | null,
      "now_utc": str,
      "now_et": str,
      "reason": str
    }
    """
    _ = input_json
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    is_weekend = now_et.weekday() >= 5

    if is_weekend:
        return json.dumps(
            {
                "can_trade_now": False,
                "is_weekend": True,
                "active_session": None,
                "now_utc": now_utc.isoformat(),
                "now_et": now_et.isoformat(),
                "reason": "Weekend: futures trading crew is disabled.",
            }
        )

    active_session = None
    for window in _load_session_windows():
        if _in_window(now_et, window.start_hhmm, window.end_hhmm):
            active_session = window.name
            break

    if active_session is None:
        return json.dumps(
            {
                "can_trade_now": False,
                "is_weekend": False,
                "active_session": None,
                "now_utc": now_utc.isoformat(),
                "now_et": now_et.isoformat(),
                "reason": "Outside configured NY/London trading windows.",
            }
        )

    return json.dumps(
        {
            "can_trade_now": True,
            "is_weekend": False,
            "active_session": active_session,
            "now_utc": now_utc.isoformat(),
            "now_et": now_et.isoformat(),
            "reason": f"Inside configured {active_session} session window.",
        }
    )
