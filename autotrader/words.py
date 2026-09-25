"""The words and numbers this bot says, defined once.

Modules import their vocabulary from here, so each word is spelled in one
place and changing it changes it everywhere.
"""

from __future__ import annotations


def many(count, one: str, more: str = "") -> str:
    """A count with its noun: "3 requests" or "1 request", never "1 request(s)".

    These lines are quoted verbatim in logs and alerts, so they are copy, not
    formatting.
    """
    return f"{count} {one if count == 1 else (more or one + 's')}"


def span(hours: float | None) -> str:
    """How long something has been going on, in the unit that fits.

    Minutes under an hour and hours under two days, so a young watch is never
    described as "0 days".
    """
    hours = max(0.0, float(hours or 0))
    if hours < 1:
        minutes = int(round(hours * 60))
        return "under an hour" if minutes < 1 else many(minutes, "minute")
    if hours < 48:
        return many(int(round(hours)), "hour")
    return days(int(hours // 24))


def days(count: int) -> str:
    """A count of days: "2 days" or "1 day"."""
    return many(int(count), "day")


def money(amount) -> str:
    """Whole Canadian dollars with thousands separators, such as "$25,000".

    Every price shown is a whole-dollar asking price, so cents would be false
    precision.
    """
    try:
        return f"${int(round(float(amount))):,}"
    except (TypeError, ValueError):
        return ""
