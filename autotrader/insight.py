"""What the numbers mean, worked out once and published with the dashboard.

Deal scores, the event feed, schedule coverage and market summaries are
derived here, so the page never has to answer them from incomplete data.
Nothing here scrapes or writes: it reads stored state and derives, so it can
be run over a fixture and checked.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any, Iterable

from . import clock, words
from .events import delivery_state
from .listing import name_of

# Below this many comparables a car is not scored, and the page says why.
MIN_COMPARABLES = 6
# A percentage is a claim about a market, so it needs more peers than a rank.
# Between the two thresholds a car gets its rank in the cohort.
MIN_FOR_A_PERCENTAGE = 12
# How long a car must be watched before "no price cut" says anything about it.
BACKTEST_MIN_DAYS = 1
# Half-width of the fixed year bands comparables are grouped in.
YEAR_BAND = 1
# A car more than this far from the median either way is worth pointing at.
NOTABLE_PCT = 8.0

KINDS = ("new", "price_drop", "price_rise", "priced", "removed", "relisted")


#: The shared parser, so a stamp means the same thing to the page, the
#: ledger and the state file.
_dt = clock.parse


def _hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


# ---------------------------------------------------------------- comparables

def _group_key(entry: dict[str, Any]) -> tuple[str, str] | None:
    make = str(entry.get("make") or "").strip().lower()
    model = str(entry.get("model") or "").strip().lower()
    if not make or not model:
        return None
    return (make, model)


def comparables(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each car, how its price sits against others of its kind.

    Which answer a car gets depends only on how many peers it has, never on
    how interesting the number would be:

      at least MIN_FOR_A_PERCENTAGE   a percentage against the cohort median
      at least MIN_COMPARABLES        its rank in the cohort, no percentage
      fewer                           why not, naming the bot's limit

    A rank is a true statement about the sample in hand; a percentage is a
    claim about a market, and on a small cohort it swings with whichever few
    cars happen to be listed. Each cohort has one median, quoted identically
    on every car in it. Cars that never reach the pool get a row saying why.
    """
    # Read once: the second pass below would see nothing if a caller passed
    # a generator.
    entries = list(entries)
    # Only cars the rules keep. Judged against cars the rules exclude, a car
    # is measured against a market nobody is shopping in.
    pool = [e for e in entries
            if e.get("price") and e.get("year") and _group_key(e)
            and e.get("status") == "active" and not e.get("filtered")]

    # One cohort per (make, model, year band), so every car in it is quoted
    # the same median.
    cohorts: dict[tuple, list[dict[str, Any]]] = {}
    for entry in pool:
        key = (_group_key(entry), year_band(entry["year"]))
        cohorts.setdefault(key, []).append(entry)

    out: dict[str, dict[str, Any]] = {}
    for entry in pool:
        key = (_group_key(entry), year_band(entry["year"]))
        cohort = cohorts[key]
        odo = entry.get("mileage_km")
        # Peers must match on wear as well as on name, or a high-mileage car
        # reads as cheap simply for carrying more kilometres.
        peers = [p for p in cohort if p["id"] != entry["id"]
                 and _similar_wear(odo, p.get("mileage_km"))]
        prices = sorted(int(p["price"]) for p in peers)
        row: dict[str, Any] = {"sample": len(prices), "band": YEAR_BAND,
                               "cohort": _cohort_name(entry)}

        if len(prices) >= MIN_FOR_A_PERCENTAGE:
            everyone = sorted([int(p["price"]) for p in peers] + [int(entry["price"])])
            median = statistics.median(everyone)
            row["median"] = int(median)
            row["delta"] = int(entry["price"]) - int(median)
            row["pct"] = round((entry["price"] - median) / median * 100.0, 1)
            row["notable"] = abs(row["pct"]) >= NOTABLE_PCT
            row["cheaper_than"] = sum(1 for p in prices if p > entry["price"])
            odos = [p.get("mileage_km") for p in peers if p.get("mileage_km")]
            row["peer_median_km"] = int(statistics.median(odos)) if odos else None
        elif len(prices) >= MIN_COMPARABLES:
            # A rank, which is true of this sample, instead of a percentage,
            # which would be a claim about a market this size cannot support.
            everyone = sorted([int(p["price"]) for p in peers] + [int(entry["price"])])
            row["rank"] = everyone.index(int(entry["price"])) + 1
            row["of"] = len(everyone)
            row["why_not"] = (
                f"{_ordinal(row['rank'])} cheapest of {row['of']} "
                f"{row['cohort']} here - too few to call it cheap or dear")
        else:
            row["why_not"] = _no_cohort(entry, len(prices), cohort)
        out[str(entry["id"])] = row

    # A row for every car that never reached the pool, saying which
    # precondition it failed, so a blank never stands for "never looked".
    seen = {str(e["id"]) for e in pool}
    for entry in entries:
        key = str(entry.get("id") or "")
        if not key or key in seen:
            continue
        out[key] = {"sample": 0, "why_not": _never_compared(entry)}
    return out


def _never_compared(entry: dict[str, Any]) -> str:
    """Why a car was not put up against any other."""
    if entry.get("status") != "active":
        return ("it has left the market - the last price the bot saw is the "
                "one recorded above, and it is not compared against cars "
                "still for sale")
    if entry.get("filtered"):
        return ("a rule of yours hides it, and the comparison is made only "
                "against the cars you are actually shopping")
    if not entry.get("price"):
        return "it has no asking price, so there is nothing to compare"
    if not entry.get("year"):
        return ("its model year did not parse, and the comparison is made "
                "within a year band")
    return ("its make and model did not parse, so it has no cohort on this "
            "dashboard")


def _similar_wear(mine: Any, theirs: Any, tolerance: float = 0.35) -> bool:
    """Close enough on the odometer to be the same kind of car.

    A car with no reading is never excluded: dropping a peer for a missing
    field shrinks the sample that decides whether there is a sample at all.
    """
    try:
        mine, theirs = int(mine), int(theirs)
    except (TypeError, ValueError):
        return True
    if mine <= 0 or theirs <= 0:
        return True
    return abs(theirs - mine) <= max(mine, theirs) * tolerance


def year_band(year: Any) -> tuple[int, int] | None:
    """The fixed band of years a car is compared inside, as (first, last).

    Fixed bands rather than a window around each car, so every car in a band
    is quoted the same median. The cost is that two cars a year apart can
    fall in different bands.
    """
    try:
        value = int(year)
    except (TypeError, ValueError):
        return None
    width = YEAR_BAND * 2 + 1
    first = value // width * width
    return first, first + width - 1


def _cohort_name(entry: dict[str, Any]) -> str:
    """What the car is compared against, naming the year band actually used."""
    make = str(entry.get("make") or "").strip()
    model = str(entry.get("model") or "").strip()
    band = year_band(entry.get("year"))
    span = f"{band[0]}-{band[1]}" if band else ""
    return " ".join(p for p in (make, model, span) if p).strip() or "cars like it"


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _no_cohort(entry: dict[str, Any], peers: int, cohort: list[dict[str, Any]]) -> str:
    """Why this car is not scored, naming the bot's limit, not the market's.

    A car alone in its cohort has no market on this dashboard at all (usually
    a stray result from a search for a different car), which is not the same
    as falling just short of the threshold.
    """
    name = _cohort_name(entry)
    if not peers and len(cohort) <= 1:
        return (f"nothing else here is a {name} - this car has no market on "
                f"this dashboard to be judged against")
    if peers < len(cohort) - 1:
        held = len(cohort) - 1 - peers
        return (f"{_count(peers, 'comparable')} close enough on age and "
                f"mileage to judge it against. "
                + ("One more is listed with very different kilometres on it."
                   if held == 1 else
                   f"Another {held} are listed with very different kilometres "
                   f"on them.")
                + " Not scored.")
    return (f"{_count(peers, 'comparable')} to compare it against - fewer "
            f"than the {MIN_COMPARABLES} this needs. Not scored.")


# Where price per kilometre means anything. Below the floor the odometer
# shows delivery mileage rather than wear and the ratio explodes; above the
# ceiling the asking price tracks condition rather than kilometres.
PER_KM_FLOOR = 20_000
PER_KM_CEILING = 200_000


def per_1000km(price: Any, km: Any) -> float | None:
    """Asking price per thousand kilometres, where that means something."""
    try:
        price = int(price)
        km = int(km)
    except (TypeError, ValueError):
        return None
    if price <= 0 or not (PER_KM_FLOOR <= km <= PER_KM_CEILING):
        return None
    return round(price / (km / 1000.0), 1)


def per_1000km_withheld(price: Any, km: Any) -> str | None:
    """Why this car has no price-per-kilometre, when it has none.

    Absence is never the only signal: a blank reads the same as "no
    odometer", which is a different thing.
    """
    if per_1000km(price, km) is not None:
        return None
    try:
        km = int(km)
    except (TypeError, ValueError):
        return "no odometer reading"
    try:
        if int(price) <= 0:
            return "no asking price"
    except (TypeError, ValueError):
        return "no asking price"
    if km < PER_KM_FLOOR:
        return (f"only {km:,} km on it - that is delivery mileage, not wear, "
                f"and the ratio would be meaningless")
    return (f"at {km:,} km the asking price tracks condition rather than "
            f"kilometres, so the ratio stops meaning anything")


# ---------------------------------------------------------------- the feed

def _delivery(entry: dict[str, Any]) -> dict[str, Any]:
    """Whether you heard about this, and if not, why not.

    The decision and its ordering live in events.delivery_state, shared with
    the ledger so the two cannot disagree; only the card's wording is here.
    """
    state = delivery_state(entry)
    if state == "queued":
        return {"state": state, "text": "queued for delivery, not sent yet"}
    if state == "quiet":
        text = str(entry["quiet_reason"])
        if entry.get("notified_at"):
            text += (f" (you were told about this car at "
                     f"{entry['notified_at']}, before that rule applied)")
        return {"state": state, "text": text}
    if state == "sent":
        return {"state": state, "at": entry["notified_at"],
                "text": f"sent {entry['notified_at']}"}
    return {"state": state,
            "text": "no record of telling you - which is itself a fault"}


def events(entries: Iterable[dict[str, Any]], limit: int = 400
           ) -> list[dict[str, Any]]:
    """Everything that happened to a car, newest first.

    Derived from each car's record rather than from the run counters, which
    count only cars that pass the filters, so changes on hidden cars show too.
    """
    out: list[dict[str, Any]] = []

    def add(kind: str, at: Any, entry: dict[str, Any], **extra: Any) -> None:
        when = _dt(at)
        if when is None:
            return
        out.append({
            "kind": kind,
            "at": when.isoformat(timespec="seconds"),
            "listing_id": str(entry.get("id")),
            # name_of, not entry["title"]: a hidden car is never enriched from
            # its own page, so its title can still be the parser's placeholder
            # long after make, model and year are known.
            "title": name_of(entry),
            "year": entry.get("year"),
            "make": entry.get("make"),
            "model": entry.get("model"),
            "price": entry.get("price"),
            "filtered": bool(entry.get("filtered")),
            "filter_reason": entry.get("filter_reason") or "",
            "delivery": _delivery(entry),
            **extra,
        })

    for entry in entries:
        add("new", entry.get("first_seen"), entry)

        history = entry.get("price_history") or []
        for before, after in zip(history, history[1:]):
            old, new = before.get("price"), after.get("price")
            if not old or not new or old == new:
                continue
            add("price_drop" if new < old else "price_rise", after.get("at"),
                entry, old_price=old, new_price=new, delta=new - old)

        if entry.get("priced_at"):
            add("priced", entry["priced_at"], entry,
                new_price=entry.get("price"))
        if entry.get("relisted_at"):
            add("relisted", entry["relisted_at"], entry)
        if entry.get("qualified_at"):
            add("qualified", entry["qualified_at"], entry,
                was_hidden_by=entry.get("qualified_from") or "")
        if entry.get("seller_changed_at"):
            add("seller", entry["seller_changed_at"], entry,
                was=entry.get("seller_was") or "",
                now=entry.get("seller_type") or "")
        if entry.get("photos_at"):
            add("photos", entry["photos_at"], entry,
                photos=len(entry.get("images") or []))
        if entry.get("removed_at") and entry.get("status") == "gone":
            add("removed", entry["removed_at"], entry)

    out.sort(key=lambda e: e["at"], reverse=True)
    if len(out) <= limit:
        return out

    # The cut keeps what is rare, not just what is recent. Arrivals far
    # outnumber every other kind, so a plain newest-first slice would let one
    # busy day of first sightings push every price drop off the end. Other
    # kinds are kept first and arrivals fill the rest; order is unchanged.
    rare = [e for e in out if e["kind"] != "new"][:limit]
    arrivals = [e for e in out if e["kind"] == "new"][:max(0, limit - len(rare))]
    chosen = {id(e) for e in rare} | {id(e) for e in arrivals}
    return [e for e in out if id(e) in chosen]


# ---------------------------------------------------------------- coverage

def _read_the_site(run: dict[str, Any]) -> bool:
    """Did this run actually look at the searches?

    That, not the exit code, decides whether a slot was covered. A skipped
    run (another held the lock, or it was too soon) never looked, nor did one
    whose searches all failed to load; every other run did.
    """
    if run.get("skipped"):
        return False
    ran = int(run.get("searches_run") or 0)
    failed = int(run.get("searches_failed") or 0)
    if ran:
        return failed < ran
    # No search counters on this record: fall back to the exit code rather
    # than crediting a slot nothing is known about.
    return bool(run.get("ok"))


# Triggers for runs nobody started by hand. repository_dispatch counts
# because it is the documented way to drive checks from an external timer.
SCHEDULE_TRIGGERS = frozenset({"schedule", "repository_dispatch"})


def _is_a_schedule(how: str) -> bool:
    """Whether a run's trigger is a schedule.

    A repository_dispatch may name its caller ("repository_dispatch:my-timer"),
    so only the part before the colon is matched.
    """
    return str(how or "").split(":", 1)[0] in SCHEDULE_TRIGGERS


def coverage(runs: list[dict[str, Any]], expected_minutes: int = 30,
             window_hours: int = 24, now: datetime | None = None,
             *, since_change: str | None) -> dict[str, Any]:
    """How much of the recent window was actually watched.

    The honest measure is not whether the last run worked but what fraction
    of the expected checks actually happened. Scheduled runs are not
    guaranteed to fire, and this is the number that says whether a car could
    have come and gone between checks.

    ``since_change`` has no default on purpose: it is when the current
    interval started, and a window reaching past it would credit the new
    schedule with runs made under the old one. A caller with no stamp passes
    None explicitly.
    """
    now = now or clock.now()
    start = now - timedelta(hours=window_hours)

    # A coverage figure is a statement about a schedule, so it is measured
    # only over the period that schedule has been running.
    changed = _dt(since_change)
    partial = False
    if changed is not None and changed > start:
        start = changed
        partial = True
    measured_hours = max(0.0, _hours_between(now, start))
    # Complete slots only: counting the slot in progress either flatters the
    # figure (a check has landed) or damns it (one is still due).
    import math
    complete = int(math.floor(measured_hours * 60 / max(1, expected_minutes)))
    expected = max(1, complete)

    # Runs that tried to check. A firing that stood down because a check had
    # just happened is recorded (it proves its timer is alive) but is not a
    # check, and is counted separately.
    stamps = sorted(t for t in (_dt(r.get("at")) for r in runs
                                if not r.get("skipped"))
                    if t is not None and t >= start)
    stood_down = sum(1 for r in runs
                     if r.get("skipped") and (_dt(r.get("at")) or start) >= start)

    # Covered, not ok: a run that read the site and then failed its own
    # bookkeeping still watched its slot. Only a search that failed to load
    # leaves a hole; a run that complains about itself is reported separately.
    #
    # `clean` and `complained` are both counted over the runs that read the
    # site, so a stand-down (exit zero, nothing read) cannot skew either.
    in_window = [r for r in runs
                 if (_dt(r.get("at")) or start) >= start and _dt(r.get("at"))]
    looked = [r for r in in_window if _read_the_site(r)]
    read_stamps = sorted(t for t in (_dt(r.get("at")) for r in looked) if t)
    ok_stamps = sorted(t for t in (_dt(r.get("at")) for r in looked
                                   if r.get("ok")) if t)

    # Distinct slots, not checks: two checks in one slot cover one slot, so a
    # burst of manual runs cannot report coverage the schedule never gave.
    slot = max(1, expected_minutes) * 60
    covered = {int((t - start).total_seconds() // slot) for t in read_stamps}
    covered = {i for i in covered if 0 <= i < expected}

    # The same count over only the runs the schedule started: checks from
    # pushes or hand dispatches do not say whether the watch continues with
    # nobody working on it. A run with no recorded trigger counts as
    # unattributed, the direction that cannot flatter the schedule.
    by_trigger: dict[str, int] = {}
    scheduled: set[int] = set()
    # Times the schedule fired, whether or not the firing produced a check.
    # One that stood down after a recent push still proves the cron is alive,
    # which slots_scheduled cannot see.
    fired = 0
    # Which timer filled each slot first, so the page can name the one that is
    # actually keeping time rather than reporting that something is.
    filled: dict[int, str] = {}
    for run in runs:
        when = _dt(run.get("at"))
        if when is None or when < start:
            continue
        if _is_a_schedule(str(run.get("trigger") or "")):
            fired += 1
        if not _read_the_site(run):
            continue
        how = str(run.get("trigger") or "") or "unattributed"
        by_trigger[how] = by_trigger.get(how, 0) + 1
        index = int((when - start).total_seconds() // slot)
        if 0 <= index < expected:
            filled.setdefault(index, how)
            if _is_a_schedule(how):
                scheduled.add(index)

    gaps: list[float] = []
    edge = [start] + read_stamps + [now]
    for before, after in zip(edge, edge[1:]):
        gaps.append(_hours_between(after, before) * 60.0)

    # The run history is capped, so a window that reaches past the oldest run
    # kept would report a coverage of zero for hours nobody has a record of.
    # Measuring from the first run in the window keeps the figure about what
    # happened rather than about how much history is retained.
    truncated = bool(runs) and (_dt(runs[-1].get("at")) or start) > start

    return {
        "window_hours": round(measured_hours, 1),
        "asked_window_hours": window_hours,
        # True when a schedule change cut the window short, so the page can
        # say the measurement is partial.
        "partial": partial,
        # Three slots is the fewest that can tell a schedule from an accident;
        # below that the page says how long it has been measuring instead.
        "too_short": complete < 3,
        "since_change": changed.isoformat(timespec="seconds") if changed else None,
        "expected": expected,
        "checks": len(stamps),
        # Firings that found a check too recent to repeat: deduplication, not
        # a fault, shown so two timers running together do not look like waste.
        "stood_down": stood_down,
        "successful": len(read_stamps),
        "slots_covered": len(covered),
        # Runs that read the site but reported a problem about themselves.
        # Shown next to the coverage figure rather than folded into it.
        "complained": len(read_stamps) - len(ok_stamps),
        "clean": len(ok_stamps),
        "pct": round(min(100.0, len(covered) / expected * 100.0), 1),
        "longest_gap_minutes": round(max(gaps), 1) if gaps else None,
        "expected_interval_minutes": expected_minutes,
        "truncated": truncated,
        "since": start.isoformat(timespec="seconds"),
        # One entry per expected slot, oldest first: 0 for no check, 1 for a
        # check, 2 for one that also complained about itself. The shape shows
        # whether the schedule is thin or absent for hours.
        "slots": _slot_row(runs, start, slot, expected),
        # Slots the schedule filled. `pct` counts every check; this counts
        # only the ones that would have happened with nobody watching.
        "slots_scheduled": len(scheduled),
        "pct_scheduled": round(min(100.0, len(scheduled) / expected * 100.0), 1),
        "by_trigger": by_trigger,
        # Checks landed but the schedule produced none of them, so a healthy
        # percentage must not stand for a schedule that is not running.
        "propped_up": bool(covered) and not scheduled,
        # Firings, not slots: a scheduled run that stood down still proves the
        # cron is alive. Compare with `expected` to see whether the schedule
        # is being served at all.
        "schedule_fired": fired,
        # The timer that filled the most slots, by name, so there is something
        # specific to check when it stops.
        "timekeeper": _timekeeper(filled),
        # Slots filled per timer. Counts slots, not checks: three dispatches
        # in one slot kept time once.
        "slots_by_trigger": _slots_by_trigger(filled),
    }


def _slots_by_trigger(filled: dict[int, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for how in filled.values():
        out[how] = out.get(how, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _timekeeper(filled: dict[int, str]) -> str | None:
    """Whichever timer filled the most slots, or None when none did.

    Ties go to the schedule: a working schedule is the headline, and a push
    that filled as many slots is incidental.
    """
    counts = _slots_by_trigger(filled)
    if not counts:
        return None
    best = max(counts.values())
    leaders = [how for how, n in counts.items() if n == best]
    for how in leaders:
        if _is_a_schedule(how):
            return how
    return leaders[0]


def _slot_row(runs: list[dict[str, Any]], start: datetime, slot: int,
              expected: int) -> list[int]:
    row = [0] * expected
    for run in runs:
        when = _dt(run.get("at"))
        if when is None or when < start or not _read_the_site(run):
            continue
        index = int((when - start).total_seconds() // slot)
        if 0 <= index < expected:
            row[index] = max(row[index], 1 if run.get("ok") else 2)
    return row


def minutes_spent(runs: list[dict[str, Any]], window_hours: int = 24,
                  now: datetime | None = None) -> dict[str, Any]:
    """Runner time the checks cost over the window.

    ``billed_minutes`` is the figure to quote: GitHub rounds every job up to
    a whole minute. ``minutes`` is wall clock inside the runs, which shows
    whether the checks themselves are getting slower.
    """
    now = now or clock.now()
    start = now - timedelta(hours=window_hours)
    window = [r for r in runs if (_dt(r.get("at")) or start) >= start]
    # A firing that stood down is a run, not a check: it read nothing, and
    # averaging its zero duration in would drag the mean down. It is still
    # billed, so `billed_minutes` counts every run.
    checks = [r for r in window if _read_the_site(r)]
    durations = [float(r.get("duration_s") or 0) for r in checks]
    total = sum(durations) / 60.0
    import math
    overhead = 25.0
    billed = sum(max(1, math.ceil((float(r.get("duration_s") or 0) + overhead) / 60.0))
                 for r in window)
    return {
        "checks": len(durations),
        "firings": len(window),
        "minutes": round(total, 1),
        "billed_minutes": billed,
        "mean_seconds": round(statistics.mean(durations), 1) if durations else None,
        "window_hours": window_hours,
    }


# ---------------------------------------------------------------- the week

def weekly(entries: Iterable[dict[str, Any]], runs: list[dict[str, Any]],
           days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    """What the market did this week, for a digest nobody has to decode.

    About the market rather than the bot: the per-run alerts already say what
    the bot did.
    """
    now = now or clock.now()
    start = now - timedelta(days=days)
    since = start.isoformat(timespec="seconds")
    entries = list(entries)

    window = [e for e in events(entries, limit=10_000) if e["at"] >= since]
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for event in window:
        by_kind.setdefault(event["kind"], []).append(event)

    drops = sorted(by_kind.get("price_drop", []), key=lambda e: e.get("delta") or 0)
    live = [e for e in entries if e.get("status") == "active" and e.get("price")]
    prices = sorted(int(e["price"]) for e in live)

    # A median moves when prices change or when the mix of cars changes, and
    # over a week it is usually the mix; the digest says so plainly.
    older = [e for e in entries
             if e.get("price_history") and len(e["price_history"]) > 1]
    then: list[int] = []
    for entry in older:
        for point in entry["price_history"]:
            if str(point.get("at") or "") <= since and point.get("price"):
                then.append(int(point["price"]))
                break

    out: dict[str, Any] = {
        "since": since,
        "days": days,
        "new": len(by_kind.get("new", [])),
        "gone": len(by_kind.get("removed", [])),
        "back": len(by_kind.get("relisted", [])),
        "qualified": len(by_kind.get("qualified", [])),
        "priced": len(by_kind.get("priced", [])),
        "drops": len(drops),
        "rises": len(by_kind.get("price_rise", [])),
        "cut_total": sum(abs(e.get("delta") or 0) for e in drops),
        "biggest_drop": drops[0] if drops else None,
        "live": len(live),
        "median": int(statistics.median(prices)) if prices else None,
        "checks": sum(1 for r in runs if str(r.get("at") or "") >= since and r.get("ok")),
    }
    if then and prices:
        was = int(statistics.median(sorted(then)))
        out["median_then"] = was
        out["median_move"] = out["median"] - was
    return out


def weekly_text(summary: dict[str, Any]) -> str:
    """The week as something worth reading on a phone."""
    lines = [f"The last {summary['days']} days on your searches", ""]
    if summary["new"]:
        lines.append(f"{_count(summary['new'], 'new listing')} appeared.")
    if summary["drops"]:
        total = f"${summary['cut_total']:,}"
        lines.append(f"{_count(summary['drops'], 'price drop')}, "
                     f"{total} off in total.")
        best = summary.get("biggest_drop")
        if best:
            lines.append(f"  Biggest: {best.get('title') or 'a listing'} "
                         f"${abs(best.get('delta') or 0):,} off, now "
                         f"${(best.get('new_price') or 0):,}.")
    if summary["rises"]:
        lines.append(f"{_count(summary['rises'], 'price increase')}.")
    if summary["priced"]:
        lines.append(f"{_count(summary['priced'], 'call-for-price car')} "
                     f"named a figure.")
    if summary["gone"]:
        lines.append(f"{summary['gone']} left the market.")
    if summary["back"]:
        lines.append(f"{summary['back']} came back.")
    if summary.get("qualified"):
        lines.append(f"{summary['qualified']} came back inside your rules.")
    if not any(summary[k] for k in ("new", "drops", "rises", "priced", "gone",
                                    "back", "qualified")):
        lines.append("Nothing moved. Every car is where it was.")

    lines.append("")
    if summary.get("median") is not None:
        line = f"{summary['live']} cars live, median asking ${summary['median']:,}"
        move = summary.get("median_move")
        if move:
            way = "up" if move > 0 else "down"
            line += (f" - {way} ${abs(move):,} on a week ago, though a median moves "
                     f"as much on which cars are listed as on what they cost")
        lines.append(line + ".")
    lines.append(f"Built from {_count(summary['checks'], 'successful check')} "
                 f"this week.")
    return "\n".join(lines)


# ---------------------------------------------------------------- the market

# How long a price point has to survive before compaction stops thinning it.
KEEP_DAILY_DAYS = 30


def compact_history(history: list[dict[str, Any]], *,
                    keep_daily_days: int = KEEP_DAILY_DAYS,
                    now: datetime | None = None) -> list[dict[str, Any]]:
    """Thin a price history without losing its shape.

    Every observation is kept for the first month, because that is the window
    a person is actually looking at. Older than that, one point per week is
    enough to draw the trend, and the first and last points are always kept -
    losing an endpoint would move the very numbers the history exists for.
    """
    if len(history) <= 2:
        return list(history)
    now = now or clock.now()
    cut = now - timedelta(days=keep_daily_days)

    kept: list[dict[str, Any]] = [history[0]]
    last_week: str | None = None
    for point in history[1:-1]:
        when = _dt(point.get("at"))
        if when is None:
            continue
        if when >= cut:
            kept.append(point)
            continue
        week = f"{when.isocalendar().year}-{when.isocalendar().week}"
        if week != last_week:
            kept.append(point)
            last_week = week
    kept.append(history[-1])
    return kept


def _trim_of(entry: dict[str, Any]) -> str:
    """A trim name coarse enough to group on.

    Dealer titles carry a paragraph of options, and only a few trim words
    matter for price. Anything else is "base" rather than an invented
    category.
    """
    import unicodedata
    raw = " ".join(str(entry.get(k) or "") for k in ("trim", "title"))
    # Fold accents first, so an accented spelling of a trim word still matches.
    text = "".join(c for c in unicodedata.normalize("NFKD", raw)
                   if not unicodedata.combining(c)).lower()
    for word in ("competition", "touring", "cs", "m carbon", "lci"):
        if word in text:
            return word.replace("m carbon", "carbon")
    return "base"


def _count(n: int, word: str, plural: str = "") -> str:
    """A count with its noun correctly pluralised, never "price drop(s)"."""
    return f"{n} {word if n == 1 else (plural or word + 's')}"


#: Shared phrasing lives in autotrader.words.
_days = words.days
_span = words.span


# Below this many cars, the prices themselves are shown instead of a median
# drawn from them, which at these sizes is both more honest and more useful.
MIN_FOR_A_MEDIAN = 5


def _spread(prices: list[int]) -> dict[str, Any]:
    prices = sorted(prices)
    row: dict[str, Any] = {"n": len(prices)}
    if len(prices) < MIN_FOR_A_MEDIAN:
        row["prices"] = prices
        row["thin"] = True
        return row
    row.update({
        "median": int(statistics.median(prices)),
        "low": prices[0], "high": prices[-1],
        "q1": int(statistics.quantiles(prices, n=4)[0]) if len(prices) >= 4 else None,
        "q3": int(statistics.quantiles(prices, n=4)[2]) if len(prices) >= 4 else None,
        "thin": False,
    })
    return row


def _model_label(entry: dict[str, Any]) -> str:
    make = str(entry.get("make") or "").strip()
    model = str(entry.get("model") or "").strip()
    return " ".join(p for p in (make, model) if p) or "unknown"


def _by_model(live: list[dict[str, Any]]) -> dict[str, Any]:
    """Every model on its own, with year and trim breakdowns inside it.

    Sorted by how many cars each has, so the page leads with the one there is
    something to say about.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in live:
        if entry.get("price"):
            groups.setdefault(_model_label(entry), []).append(entry)

    out: dict[str, Any] = {}
    for label, cars in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        years: dict[int, list[int]] = {}
        trims: dict[str, list[int]] = {}
        for car in cars:
            if car.get("year"):
                years.setdefault(int(car["year"]), []).append(int(car["price"]))
            trims.setdefault(_trim_of(car), []).append(int(car["price"]))
        row = _spread([int(c["price"]) for c in cars])
        row["label"] = label
        row["by_year"] = {str(y): _spread(p) for y, p in sorted(years.items())}
        row["by_trim"] = {t: _spread(p) for t, p in
                          sorted(trims.items(), key=lambda kv: -len(kv[1]))}
        out[label] = row
    return out


def market(entries: Iterable[dict[str, Any]], *, now: datetime | None = None,
           runs: Iterable[dict[str, Any]] | None = None,
           since: str | None = None) -> dict[str, Any]:
    """What the whole dataset says, rather than what one car says.

    Every figure carries its sample size, and anything drawn from fewer than
    a handful of cars is left out rather than rounded into a number that
    looks authoritative.
    """
    now = now or clock.now()
    entries = list(entries)
    live = [e for e in entries if e.get("status") == "active"]
    gone = [e for e in entries if e.get("status") == "gone" and e.get("removed_at")]

    # ---- price by model, then by year and trim within it ------------------
    #
    # Grouped by model first: a median across two different models is not a
    # number about either of them. Only cars the rules keep are priced, so a
    # median describes the market being shopped; hidden cars are counted
    # beside each model instead.
    yours = [e for e in live if not e.get("filtered")]
    by_model = _by_model(yours)
    hidden_by_model: dict[str, int] = {}
    for entry in live:
        if entry.get("filtered"):
            label = _model_label(entry)
            hidden_by_model[label] = hidden_by_model.get(label, 0) + 1
    for label, row in by_model.items():
        row["hidden"] = hidden_by_model.pop(label, 0)
    # A model with only hidden cars still gets a line, or the page silently
    # omits a car you are searching for.
    for label, count in hidden_by_model.items():
        by_model[label] = {"label": label, "n": 0, "hidden": count,
                           "thin": True, "prices": [],
                           "by_year": {}, "by_trim": {}}
    # Flat year and trim tables for the page and the weekly digest, scoped to
    # the model with the most cars.
    leader = max(by_model.values(), key=lambda m: m["n"], default=None)
    if leader is not None and not leader["n"]:
        leader = None
    by_year = leader["by_year"] if leader else {}
    by_trim = leader["by_trim"] if leader else {}

    # ---- how long things last --------------------------------------------
    # A disappearing listing does not mean a sale. What is knowable is how
    # long a car was listed before it came down.
    lifespans: list[int] = []
    for entry in gone:
        first, last = _dt(entry.get("first_seen")), _dt(entry.get("removed_at"))
        if first and last and last > first:
            lifespans.append(max(0, (last - first).days))
    lifespans.sort()

    standing: list[int] = []
    for entry in live:
        first = _dt(entry.get("first_seen"))
        if first:
            standing.append(max(0, (now - first).days))
    standing.sort()

    # ---- how fast the market moves ---------------------------------------
    week = now - timedelta(days=7)
    arrivals = sum(1 for e in entries
                   if (_dt(e.get("first_seen")) or now) >= week)
    departures = sum(1 for e in gone if (_dt(e.get("removed_at")) or now) >= week)

    cut_total = 0
    cut_cars = 0
    for entry in entries:
        history = entry.get("price_history") or []
        drops = sum(max(0, a["price"] - b["price"])
                    for a, b in zip(history, history[1:])
                    if a.get("price") and b.get("price") and b["price"] < a["price"])
        if drops:
            cut_total += drops
            cut_cars += 1

    # How much of this is actually observation. A price can only have moved
    # on a car seen more than once, and early in a watch few have been, so a
    # "trend" would be a sentence about the sample rather than the market.
    seen = sorted(t for t in (_dt(e.get("first_seen")) for e in entries) if t)
    tracked = [e for e in entries if len(e.get("price_history") or []) > 1]
    span_days = (now - seen[0]).days if seen else 0
    watched = sorted(t for t in (_dt(e.get("first_seen")) for e in live) if t)

    # When the watch itself began, which is not when the oldest car was first
    # seen: every car live on the first run is first seen that day. The
    # censoring test below needs this independent date, or it compares a
    # number against itself.
    started = sorted(t for t in (_dt(r.get("at")) for r in (runs or [])) if t)
    # ``since`` is the state's written-once record. The run log is a rolling
    # window, so relying on it alone would make a long watch look days old.
    watch_began = (_dt(since) or (started[0] if started else None)
                   or (watched[0] if watched else now))
    watch_days = max(0, (now - watch_began).days)
    watch_hours = max(0.0, _hours_between(now, watch_began))

    return {
        "at": now.isoformat(timespec="seconds"),
        "window": {
            "ids_span_days": span_days,
            "watching_days": watch_days,
            "cars_with_two_prices": len(tracked),
            "thin": len(tracked) < 20 or watch_days < 14,
            "note": (f"{len(tracked)} of {len(entries)} cars have been priced "
                     f"more than once, over {_span(watch_hours)} of watching. "
                     f"Anything below described as a trend is really a "
                     f"snapshot until that number grows."),
        },
        "live": len(live),
        "gone": len(gone),
        "by_year": by_year,
        "by_trim": by_trim,
        "by_model": by_model,
        "leader": leader["label"] if leader else None,
        "listed_days": {
            "n": len(lifespans),
            "median": lifespans[len(lifespans) // 2] if lifespans else None,
            "p10": lifespans[len(lifespans) // 10] if len(lifespans) >= 10 else None,
            "p90": lifespans[len(lifespans) * 9 // 10] if len(lifespans) >= 10 else None,
            # Deliberately not "days to sell": a listing coming down means the
            # seller stopped advertising it, which is not the same thing.
            "note": "how long a car was listed before it came down - not how "
                    "long it took to sell, which a listing cannot tell you",
            # Only a listing that arrived and left inside the watch can be
            # measured, so a short watch sees only short lives; the page must
            # say the sample is biased.
            "biased_short": watch_days < 30,
            "watching_days": watch_days,
        },
        "still_listed_days": {
            "n": len(standing),
            "median": standing[len(standing) // 2] if standing else None,
            "longest": standing[-1] if standing else None,
            # Right-censored: a car first seen on the first run has been
            # listed for *at least* this long, and how much longer is not
            # knowable, so the floor must not be reported as the figure.
            # Measured against when these searches began, not when the bot
            # was installed, since the watch list can change. Not against
            # `min(watched)`, which is the other side of the same comparison
            # and would make this true of every dataset.
            "censored": bool(standing and watched
                             and watched[0] <= watch_began + timedelta(hours=6)),
            "watching_days": watch_days,
        },
        "velocity": {"arrived_7d": arrivals, "left_7d": departures,
                     # In a watch younger than a week every car "arrived this
                     # week", so the page says the window is the watch.
                     "window_is_the_watch": watch_days < 7},
        "discounting": {"cars": cut_cars, "total": cut_total,
                        "mean": int(cut_total / cut_cars) if cut_cars else None},
    }


def backtest(entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Did the deal score say anything useful, in hindsight?

    If the score means anything, cars it called dear should cut their prices
    more often than cars it called cheap. This checks over the dataset and
    says so either way.
    """
    entries = list(entries)
    scores = comparables(entries)
    # Cars are re-scored against today's market, so a car that has since been
    # discounted is compared at its current price. Using its first price is
    # the honest test of "was it dear when it appeared".
    cheap_cut = cheap_n = dear_cut = dear_n = 0
    for entry in entries:
        row = scores.get(str(entry.get("id")))
        if not row or row.get("pct") is None:
            continue
        history = [p for p in (entry.get("price_history") or []) if p.get("price")]
        if len(history) < 1:
            continue
        # Only cars watched long enough that a cut would have been seen. A car
        # first read today has not had the chance, and counting it fills the
        # denominator with cases that could never go the other way.
        first_seen, last_seen = _dt(entry.get("first_seen")), _dt(entry.get("last_seen"))
        if first_seen and last_seen and (last_seen - first_seen) < timedelta(
                days=BACKTEST_MIN_DAYS):
            continue
        first = history[0]["price"]
        last = history[-1]["price"]
        cut = last < first
        if row["pct"] <= -NOTABLE_PCT:
            cheap_n += 1
            cheap_cut += 1 if cut else 0
        elif row["pct"] >= NOTABLE_PCT:
            dear_n += 1
            dear_cut += 1 if cut else 0

    out: dict[str, Any] = {
        "called_cheap": cheap_n, "cheap_that_cut": cheap_cut,
        "called_dear": dear_n, "dear_that_cut": dear_cut,
    }
    if cheap_n >= 5 and dear_n >= 5:
        cheap_rate = cheap_cut / cheap_n
        dear_rate = dear_cut / dear_n
        out["cheap_rate"] = round(cheap_rate * 100, 1)
        out["dear_rate"] = round(dear_rate * 100, 1)
        out["verdict"] = (
            "cars it called dear cut their prices more often than cars it "
            "called cheap, which is what the score claims"
            if dear_rate > cheap_rate else
            "cars it called dear did not cut more often than cars it called "
            "cheap - on this data the score is not predicting anything")
    else:
        out["verdict"] = (
            f"not enough scored cars to test it: {cheap_n} called cheap and "
            f"{dear_n} called dear, and five of each is the minimum worth "
            f"drawing a conclusion from")
    return out
