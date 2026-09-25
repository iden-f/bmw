"""What this bot costs to run, and refusing to cost more than it may.

Free minutes on a public repository are one settings change away from being
billed (making it private, moving it into an organisation), so the guard
tracks what the minutes would cost rather than trusting that they are free.

Every minute is labelled when it is spent, not when the ledger is read:

    exempt    the repository was public and on a standard runner at the time
    drawing   it was not, so the minute came out of the allowance
    unknown   the bot could not tell, which counts as drawing

The label is stored in the day's row beside the number, so a later change of
visibility never relabels earlier minutes.

An allowance is metered per account and this bot sees one repository, so it
reports only its own spending and says so. GitHub rounds every job up to a
whole minute, so minutes are counted per job here too.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import clock

# Only a fallback: the plan's included minutes belong in config.json (GitHub
# Free includes 2,000 a month, Pro 3,000).
DEFAULT_ALLOWANCE = 3000

# Stop at this share of the allowance, not at 100%: the projection is a
# straight line through an unfinished month, so leave room for it to be wrong.
DEFAULT_STOP_AT = 0.85

# With fewer days of data than this, a month-end projection is arithmetic
# rather than evidence: one busy first day would project a busy month.
MIN_DAYS_TO_PROJECT = 2

STOP_FILE = "BUDGET-STOP"

# The only labels a minute can carry. "unknown" counts as drawing wherever a
# decision is made: wrongly assuming a charge costs a sentence on the
# dashboard, wrongly assuming none costs money.
EXEMPT, DRAWING, UNKNOWN = "exempt", "drawing", "unknown"

# Runner labels GitHub does not bill on a public repository; a larger runner
# is billed even there. tests/test_budget.py checks that every runs-on in this
# repository is on this list.
FREE_ON_PUBLIC = frozenset({
    "ubuntu-latest", "ubuntu-24.04", "ubuntu-22.04", "ubuntu-20.04",
    "windows-latest", "windows-2025", "windows-2022", "windows-2019",
    "macos-latest", "macos-15", "macos-14", "macos-13",
})


def _month_of(when: datetime) -> str:
    return f"{when.year:04d}-{when.month:02d}"


def _empty_day() -> dict[str, float]:
    return {EXEMPT: 0.0, DRAWING: 0.0, UNKNOWN: 0.0}


def _as_day(raw: Any) -> dict[str, float]:
    """One day's row, from either a labelled dict or a bare number.

    A bare number carries no label, so it counts as "unknown" rather than
    being labelled after the fact.
    """
    day = _empty_day()
    if isinstance(raw, dict):
        for key in (EXEMPT, DRAWING, UNKNOWN):
            try:
                day[key] = round(float(raw.get(key) or 0.0), 2)
            except (TypeError, ValueError):
                day[key] = 0.0
        return day
    try:
        day[UNKNOWN] = round(float(raw), 2)
    except (TypeError, ValueError):
        pass
    return day


@dataclass
class Ledger:
    """Minutes used this month, labelled by day, and what that projects to."""

    month: str = ""
    days: dict[str, dict[str, float]] = field(default_factory=dict)
    runs: int = 0
    allowance: int = DEFAULT_ALLOWANCE
    stop_at: float = DEFAULT_STOP_AT
    # How the next minute will be labelled, and why. Never applied to minutes
    # already recorded.
    label: str = UNKNOWN
    why: str = "nothing has told this bot what kind of repository it is in"

    def _total(self, key: str) -> float:
        return round(sum(d.get(key, 0.0) for d in self.days.values()), 2)

    @property
    def exempt(self) -> float:
        return self._total(EXEMPT)

    @property
    def unknown(self) -> float:
        return self._total(UNKNOWN)

    @property
    def drawing(self) -> float:
        """Minutes that came out of the allowance, unlabelled ones included."""
        return round(self._total(DRAWING) + self.unknown, 2)

    @property
    def used(self) -> float:
        """Every minute this bot spent, whoever paid for it."""
        return round(self.exempt + self._total(DRAWING) + self.unknown, 2)

    @property
    def days_elapsed(self) -> int:
        """Days of the month that produced data, not the day of the month.

        A bot installed mid-month has not spent the days before it arrived.
        """
        return max(1, len(self.days))

    def days_in_month(self, now: datetime) -> int:
        return calendar.monthrange(now.year, now.month)[1]

    def days_remaining(self, now: datetime) -> int:
        return max(0, self.days_in_month(now) - now.day)

    def days_to_reset(self, now: datetime) -> int:
        """Until the allowance refills. The day the month turns counts."""
        return self.days_in_month(now) - now.day + 1

    def per_day(self) -> float:
        return round(self.drawing / self.days_elapsed, 2)

    def projected(self, now: datetime) -> float | None:
        """Allowance-drawing minutes by month end, or None if it is too early."""
        if self.days_elapsed < MIN_DAYS_TO_PROJECT:
            return None
        return round(self.drawing + self.per_day() * self.days_remaining(now), 1)

    def ceiling(self) -> float:
        return round(self.allowance * self.stop_at, 1)

    # The caveat the verdict carries. Minutes are recorded in the runner's
    # `finally`, so even a crashed run is counted, but a job killed outright
    # (a timeout, a lost runner, an out-of-memory kill) is billed and never
    # seen here. The ledger is a floor, not a total, and says so.
    BLIND_SPOT = (
        "This is what THIS repository spent. The allowance has one meter per "
        "account and this bot can see one repository, so it cannot tell you "
        "how much of the allowance is left. It is also a floor rather than a "
        "total: a job killed outright - a timeout, a lost runner - is billed "
        "by GitHub and never reaches this count."
    )

    def _short(self, projection: float | None) -> str:
        """One line for a tile. No caveat, no blind spot, no arithmetic."""
        drawing, exempt = self.drawing, self.exempt
        if not drawing:
            return (f"{exempt:,.0f} minute{'' if exempt == 1 else 's'} spent, "
                    f"none of {'it' if exempt == 1 else 'them'} on the allowance")
        # The tile already shows used / allowance in large type above this
        # line, so the line adds only what that figure does not say.
        bits = []
        if projection is not None:
            bits.append(f"about {projection:,.0f} by month end")
        if self.unknown and self.unknown >= drawing:
            # All of it unlabelled: say that instead of the total again.
            bits.append("none of it labelled, so all of it counted as drawing")
        elif self.unknown:
            bits.append(f"{self.unknown:,.0f} of them unlabelled, "
                        f"counted as drawing")
        if exempt:
            bits.append(f"{exempt:,.0f} more ran exempt")
        return " \u00b7 ".join(bits) or f"of {self.allowance:,} this month"

    def verdict(self, now: datetime) -> dict[str, Any]:
        """Where this month stands, in numbers and in a sentence."""
        projection = self.projected(now)
        drawing, exempt = self.drawing, self.exempt
        over = projection is not None and projection > self.ceiling()
        spent_out = drawing >= self.ceiling()

        if not drawing:
            # A fact about this repository only: other repositories on the
            # account can still exhaust the allowance.
            state = "exempt"
            text = (f"{exempt:,.0f} runner minute"
                    f"{'' if exempt == 1 else 's'} this month, none of "
                    f"{'it' if exempt == 1 else 'them'} drawing on the "
                    f"allowance. {self.why.capitalize()}. " + self.BLIND_SPOT)
        elif spent_out:
            state = "stop"
            text = (f"{drawing:,.0f} of {self.allowance:,} allowance minutes "
                    f"are gone this month, past the {self.ceiling():,.0f} this "
                    f"bot allows itself. It has stopped checking.")
        elif over:
            state = "over"
            text = (f"At {self.per_day():,.1f} allowance minutes a day this "
                    f"month ends at about {projection:,.0f}, past the "
                    f"{self.ceiling():,.0f} this bot allows itself out of "
                    f"{self.allowance:,}. It will stop before it gets there.")
        elif projection is None:
            state = "early"
            text = (f"{drawing:,.1f} allowance minutes over "
                    f"{self.days_elapsed} day"
                    f"{'' if self.days_elapsed == 1 else 's'} - too little to "
                    f"project a month from. " + self.BLIND_SPOT)
        else:
            state = "ok"
            text = (f"{drawing:,.0f} allowance minutes used, about "
                    f"{projection:,.0f} by month end against {self.allowance:,}. "
                    + self.BLIND_SPOT)

        if exempt and drawing:
            text += (f" A further {exempt:,.0f} minute"
                     f"{'' if exempt == 1 else 's'} ran exempt.")
        if self.unknown:
            text += (f" {self.unknown:,.0f} minute"
                     f"{'' if self.unknown == 1 else 's'} could not be "
                     f"labelled and {'is' if self.unknown == 1 else 'are'} "
                     f"counted as drawing.")

        return {
            "month": self.month,
            "runs": self.runs,
            "days_elapsed": self.days_elapsed,
            "days_to_reset": self.days_to_reset(now),
            "exempt_minutes": exempt,
            "drawing_minutes": drawing,
            "unknown_minutes": self.unknown,
            "used": self.used,
            "per_day": self.per_day(),
            "projected": projection,
            "allowance": self.allowance,
            "ceiling": self.ceiling(),
            "label": self.label,
            "why": self.why,
            "blind_spot": self.BLIND_SPOT,
            # The tile's line, without the caveat: the page prints the caveat
            # once, under the row, where it covers every figure in it.
            "short": self._short(projection),
            "state": state,
            "text": text,
            # Whether the bot's own guard lets the next check run. An
            # exhausted allowance does not stop an exempt repository, so
            # this is not GitHub's limit.
            "can_still_run": state != "stop",
            "should_stop": state == "stop",
        }


def load(state: Any, cfg: Any = None, *, now: datetime | None = None) -> Ledger:
    """The ledger for the current month, starting fresh when the month turns."""
    now = now or clock.now()
    raw = dict((state.data.get("actions") or {}))
    month = _month_of(now)
    if raw.get("month") != month:
        # A new month is a new allowance; last month's rows are not worth
        # keeping in a file every run rewrites.
        raw = {"month": month, "days": {}, "runs": 0}
    allowance, stop_at = DEFAULT_ALLOWANCE, DEFAULT_STOP_AT
    if cfg is not None:
        allowance = int(cfg.get("budget.included_minutes", DEFAULT_ALLOWANCE)
                        or DEFAULT_ALLOWANCE)
        stop_at = float(cfg.get("budget.stop_at", DEFAULT_STOP_AT)
                        or DEFAULT_STOP_AT)
    label, why = label_for(cfg, state)
    days = {day: _as_day(v) for day, v in (raw.get("days") or {}).items()}
    return Ledger(month=month, days=days, runs=int(raw.get("runs") or 0),
                  allowance=allowance, stop_at=stop_at, label=label, why=why)


def label_for(cfg: Any, state: Any) -> tuple[str, str]:
    """How to label the minutes this run is about to spend, and why.

    The evidence comes with the label so the claim can be checked rather than
    taken on trust.
    """
    told = None if cfg is None else cfg.get("budget.charged", None)
    if told is not None:
        return ((DRAWING, "config.json says these minutes are charged")
                if told else
                (EXEMPT, "config.json says these minutes are not charged"))

    repo = {}
    try:
        repo = dict(state.data.get("repo") or {})
    except (AttributeError, TypeError, ValueError):
        repo = {}
    visibility = str(repo.get("visibility") or "").strip().lower()
    runner = str(repo.get("runner") or "").strip().lower()

    if not visibility:
        return UNKNOWN, ("nothing has told this bot whether its repository is "
                         "public - it is not running inside Actions, or the "
                         "workflow stopped passing the answer through")
    if visibility != "public":
        return DRAWING, (f"GitHub reports this repository as {visibility}, and "
                         f"Actions on a {visibility} repository come out of "
                         f"the allowance")
    if runner and runner not in FREE_ON_PUBLIC:
        # The exemption depends on the runner as well as the repository: a
        # larger runner is billed even on a public one.
        return DRAWING, (f"this repository is public, but {runner} is not one "
                         f"of the standard runners GitHub gives away")
    return EXEMPT, ("GitHub reports this repository as public and its jobs run "
                    "on standard runners, which GitHub does not bill")


def draws_on_the_allowance(cfg: Any, state: Any) -> bool:
    """Will the next minute come out of the allowance? Unknown counts as yes.

    For callers that need only the decision; label_for also gives the evidence.
    """
    return label_for(cfg, state)[0] != EXEMPT


def record(state: Any, minutes: float, *, cfg: Any = None,
           now: datetime | None = None) -> dict[str, Any]:
    """Add this run's minutes to the month and return where that leaves it."""
    now = now or clock.now()
    ledger = load(state, cfg, now=now)
    day = now.date().isoformat()
    row = ledger.days.setdefault(day, _empty_day())
    # Never negative, and never zero: a job that ran at all cost a minute.
    # Labelled with what is known now, and never relabelled.
    row[ledger.label] = round(row.get(ledger.label, 0.0) + max(1.0, minutes), 2)
    ledger.runs += 1
    state.data["actions"] = {"month": ledger.month, "days": ledger.days,
                             "runs": ledger.runs}
    return ledger.verdict(now)


def minutes_for(seconds: float, jobs: int = 1) -> float:
    """What GitHub charges for a job of this length: whole minutes, rounded up.

    A 34-second job costs one minute, not 0.57; rounding optimistically would
    let the guard report all is well while the bill says otherwise.
    """
    import math
    per_job = max(1, math.ceil(max(0.0, float(seconds)) / 60.0))
    return float(per_job * max(1, int(jobs)))
