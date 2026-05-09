"""Shared operator-facing context for agent prompts."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import load_config


def build_operator_appendix() -> str:
    """Return shared operator rules for timezones and trading mode."""
    cfg = load_config()
    now_utc = datetime.now(tz=ZoneInfo("UTC"))
    now_phx = now_utc.astimezone(ZoneInfo("America/Phoenix"))
    mode = cfg.mode
    return (
        "\n\nOperator rules you must follow:\n"
        f"- The operator is in America/Phoenix. When speaking to the operator, prefer Phoenix time "
        f"and explicitly label it as PHX time. Current PHX time: {now_phx.isoformat()}.\n"
        f"- Backend logs, database timestamps, timers, and internal records may remain in UTC. "
        f"Current UTC time: {now_utc.isoformat()}.\n"
        "- If you mention both, present PHX time first and UTC second.\n"
        f"- Trading mode is {mode.upper()} only. Everything here is IBKR paper trading, not live trading.\n"
        "- Never imply live execution, live funds, or real-money deployment unless the codebase is explicitly changed "
        "away from paper mode, which it is not.\n"
        "- If you discuss schedules, session windows, or deadlines with the operator, translate them to PHX time "
        "unless the operator explicitly asks for UTC or ET.\n"
    )
