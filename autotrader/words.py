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
    described as "0 days". Under half a minute is "under a minute", as the
    page says it.

    It follows a verb ("held 3 hours") or comes before "ago". After "over" or
    "in the last" it can make no sense: see the_last.
    """
    hours = max(0.0, float(hours or 0))
    if hours < 1:
        minutes = int(round(hours * 60))
        return "under a minute" if minutes < 1 else many(minutes, "minute")
    if hours < 48:
        return many(int(round(hours)), "hour")
    return days(int(hours // 24))


def the_last(hours: float | None) -> str:
    """"the last 3 hours", for a span that runs up to now.

    "In the last under a minute" and "in the last 1 hour" are not how anyone
    says it.
    """
    text = span(hours)
    if text in ("under a minute", "1 minute"):
        return "the last minute"
    if text == "1 hour":
        return "the last hour"
    return f"the last {text}"


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
