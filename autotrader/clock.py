"""One clock for the whole bot, always timezone-aware UTC.

Every timestamp comes from here, so a run cannot disagree with itself about
when it happened, and the test suite can run as if it were another date: a
test that quietly depends on today's date fails in the gate, not months later.

AUTOTRADER_NOW overrides the time, for that gate only. It is read on every
call rather than cached, so a test can move time forward inside one process.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

#: Set by tests that need to hold time still or move it. Takes precedence over
#: the environment so a fixture can nest inside the date gate.
_FROZEN: datetime | None = None

ENV_VAR = "AUTOTRADER_NOW"


def _from_env() -> datetime | None:
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # Falling back to the real time would let the gate pass while
        # testing nothing, so a malformed override is an error.
        raise ValueError(
            f"{ENV_VAR}={raw!r} is not an ISO timestamp. Use e.g. "
            f"2026-12-31T23:59:00Z") from None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def now() -> datetime:
    """The current moment, always timezone-aware and always UTC."""
    if _FROZEN is not None:
        return _FROZEN
    return _from_env() or datetime.now(timezone.utc)


def stamp(when: datetime | None = None) -> str:
    """The ISO-8601 string this bot writes into state, to the second.

    These stamps are compared, sorted and read by people; microseconds would
    only add noise.
    """
    return (when or now()).isoformat(timespec="seconds")


def freeze(when: datetime | str | None) -> None:
    """Hold time still. Pass None to let it run again."""
    global _FROZEN
    if isinstance(when, str):
        when = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    _FROZEN = when


def advance(delta: timedelta) -> datetime:
    """Move frozen time forward. Only meaningful while frozen."""
    global _FROZEN
    if _FROZEN is None:
        raise RuntimeError("advance() needs a frozen clock; call freeze() first")
    _FROZEN = _FROZEN + delta
    return _FROZEN


# ------------------------------------------------- how long ago was that

def parse(value: object) -> datetime | None:
    """A stored stamp as an aware UTC datetime, or None if it is not one.

    Always aware, so comparing it with `now()` cannot raise TypeError.
    Everything this bot writes is UTC, so a stamp without an offset (from a
    hand-edited state file, say) is read as UTC, not as local time.
    """
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def minutes_since(value: object, at: datetime | None = None) -> float | None:
    """Minutes between a stored stamp and now, or None if it is not a stamp.

    Never negative: a runner with a skewed clock can write a stamp in the
    future, and clamping at zero makes it read as "just now" instead of
    turning every age comparison the wrong way round.
    """
    when = parse(value)
    if when is None:
        return None
    return max(0.0, ((at or now()) - when).total_seconds() / 60.0)


def hours_since(value: object, at: datetime | None = None) -> float | None:
    """Hours between a stored stamp and now, or None if it is not a stamp."""
    mins = minutes_since(value, at)
    return None if mins is None else mins / 60.0
