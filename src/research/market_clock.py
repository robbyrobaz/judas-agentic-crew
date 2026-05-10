"""Build the TIME & MARKET STATE banner every agent sees at cycle start.

Single source of truth — Operator + Researcher + Trader + Registrar +
Coder all get this banner injected as a second system message so they
cannot ignore market hours or bar-close timing when reasoning. Also
exposed as a callable tool (`get_market_state`) so agents can re-check
during long cycles.

The CME globex schedule is the same one ``session_tools._globex_open_now``
encodes; we reuse it for correctness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

from src.tools.session_tools import _globex_open_now  # type: ignore[attr-defined]


_ET = ZoneInfo("America/New_York")
_CT = ZoneInfo("America/Chicago")
_PHX = ZoneInfo("America/Phoenix")


def _next_hourly_close_utc(now: datetime) -> datetime:
    """Top of the next UTC hour — 1H bars are aligned to UTC hour boundaries."""
    base = now.replace(minute=0, second=0, microsecond=0)
    if now > base:
        return base + timedelta(hours=1)
    return base


def _next_5m_close_utc(now: datetime) -> datetime:
    """Next 5-minute boundary in UTC."""
    minute_floor = (now.minute // 5) * 5
    base = now.replace(minute=minute_floor, second=0, microsecond=0)
    return base + timedelta(minutes=5)


def _minutes_between(a: datetime, b: datetime) -> int:
    return max(0, int((b - a).total_seconds() // 60))


def get_market_state(*, now_utc: datetime | None = None) -> dict[str, Any]:
    """Structured snapshot of right-now time + CME futures market state."""
    now = now_utc or datetime.now(timezone.utc)
    now_et = now.astimezone(_ET)
    now_ct = now.astimezone(_CT)
    now_phx = now.astimezone(_PHX)

    market_open_now, market_reason = _globex_open_now(now_et)

    next_1h = _next_hourly_close_utc(now)
    next_5m = _next_5m_close_utc(now)

    return {
        "now_utc": now.isoformat(),
        "now_et": now_et.isoformat(),
        "now_ct": now_ct.isoformat(),
        "now_phx": now_phx.isoformat(),
        "weekday": now_et.strftime("%A"),
        "cme_globex": {
            "open_now": market_open_now,
            "reason": market_reason,
        },
        "bars": {
            "next_1h_close_utc": next_1h.isoformat(),
            "minutes_to_next_1h_close": _minutes_between(now, next_1h),
            "next_5m_close_utc": next_5m.isoformat(),
            "minutes_to_next_5m_close": _minutes_between(now, next_5m),
        },
    }


def format_market_banner(*, now_utc: datetime | None = None) -> str:
    """One-paragraph banner ready to drop into a prompt as a system message."""
    s = get_market_state(now_utc=now_utc)
    cme = s["cme_globex"]
    bars = s["bars"]
    bar_close_phrase = (
        f"Next 1H bar closes at {bars['next_1h_close_utc'][:16]}Z "
        f"(in {bars['minutes_to_next_1h_close']} min). "
        f"Next 5m bar closes in {bars['minutes_to_next_5m_close']} min."
    )
    if cme["open_now"]:
        market_phrase = f"CME futures: OPEN. {cme['reason']}"
    else:
        market_phrase = f"CME futures: CLOSED. {cme['reason']}"
    return (
        f"TIME & MARKET STATE (right now — re-read this before flagging timing bugs):\n"
        f"  - UTC: {s['now_utc'][:19]}Z ({s['weekday']})\n"
        f"  - America/New_York: {s['now_et'][:19]}\n"
        f"  - America/Chicago:  {s['now_ct'][:19]}\n"
        f"  - America/Phoenix:  {s['now_phx'][:19]}\n"
        f"  - {market_phrase}\n"
        f"  - {bar_close_phrase}\n"
        f"\n"
        f"REASONING RULES:\n"
        f"  1. 'Zero trades despite signals' is NOT a bug if the market was closed\n"
        f"     during the signal window, OR if no bar has closed since the signal.\n"
        f"  2. CME futures close Fri 16:00 CT (~21:00 UTC) and reopen Sun 17:00 CT\n"
        f"     (~22:00 UTC). Weekdays have a daily break 16:00-17:00 CT.\n"
        f"  3. 1H strategies fire on the next 1H bar close; 5m on the next 5m close.\n"
        f"     Check minutes_to_next_*_close before flagging execution issues.\n"
    )


# --- Tool factory for agent palettes -----------------------------------------


def make_get_market_state(*, db_path: str = ""):
    """Factory matching the agent_tools convention. db_path unused but kept
    for signature parity. Returns (callable, schema_dict)."""

    def get_market_state_tool(**_kwargs: Any) -> dict[str, Any]:
        return get_market_state()

    schema = {
        "type": "function",
        "function": {
            "name": "get_market_state",
            "description": (
                "Return the current UTC/ET/CT/PHX time, CME futures market "
                "open/closed state, and minutes until the next 1H and 5m bar "
                "closes. Use this before flagging any timing or execution bug."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    return get_market_state_tool, schema
