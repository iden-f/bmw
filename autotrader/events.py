"""A standing record of the first time each kind of market event really happens.

Tests prove each kind against replayed payloads; this records the first real
occurrence on the market: the car, the before and after, and what was
delivered about it. It only reads what the watcher has stored - it never
scrapes, notifies or changes the bot's state - so it cannot hold up a check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import budget, clock, words
from .state import retired_by_owner

LEDGER_PATH = Path("EVENTS.md")
DATA_PATH = Path("docs/events.json")

KINDS = {
    "price_drop": "a price came down",
    "price_rise": "a price went up",
    "removed": "a car left the market",
    "relisted": "a car came back",
    "qualified": "a car came back inside your rules",
    "priced": "a call-for-price car named a figure",
}


@dataclass
class Event:
    kind: str
    at: str
    listing_id: str
    title: str = ""
    url: str = ""
    before: Any = None
    after: Any = None
    delivered: str = ""
    detail: str = ""
    run: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "at": self.at, "listing_id": self.listing_id,
                "title": self.title, "url": self.url, "before": self.before,
                "after": self.after, "delivered": self.delivered,
                "detail": self.detail, "run": self.run}


# What can have happened about a car, in the order it must be asked. A car can
# carry both a delivery time (announced once) and a quiet reason (hidden by a
# later rule), so quiet is asked before sent. insight.py makes the same
# decision in the dashboard's wording.
DELIVERY_STATES = ("queued", "quiet", "sent", "none")


def delivery_state(entry: dict[str, Any]) -> str:
    """Which of DELIVERY_STATES applies, asked in that order."""
    if entry.get("pending"):
        return "queued"
    if entry.get("quiet_reason"):
        return "quiet"
    if entry.get("notified_at"):
        return "sent"
    return "none"


def _delivery(entry: dict[str, Any]) -> str:
    """What the bot did about this car, in the words it recorded at the time."""
    state = delivery_state(entry)
    if state == "queued":
        return "queued, not yet delivered"
    if state == "quiet":
        text = f"deliberately quiet: {entry['quiet_reason']}"
        if entry.get("notified_at"):
            text += (f" (this car was announced at {entry['notified_at']}, "
                     f"before that rule applied)")
        return text
    if state == "sent":
        stamp = entry["notified_at"]
        if entry.get("notified_at_backfilled"):
            return f"delivered (time reconstructed, {stamp})"
        return f"delivered at {stamp}"
    return "no record - which is itself a fault the run should have caught"


def told_about(entry: dict[str, Any], at: Any, until: Any) -> str | None:
    """When you were told about the event at ``at``, as far as is known.

    delivery_state reads the car as it is now, which answers for its newest
    event only. For an earlier one this reads the car's record of alert
    times (State.mark_notified keeps it): the first alert at or after ``at``
    and before ``until``, the car's next event. "" when the record covers
    that stretch and no alert went out; None when it cannot say, because the
    car keeps no record or it starts later.
    """
    told = [t for t in (clock.parse(x) for x in (entry.get("told") or [])) if t]
    start, end = clock.parse(at), clock.parse(until)
    if not told or start is None:
        return None
    hits = [t for t in told if t >= start and (end is None or t < end)]
    if hits:
        return min(hits).isoformat(timespec="seconds")
    since = clock.parse(entry.get("told_since"))
    if since is not None and start < since:
        return None
    return ""


def event_times(entry: dict[str, Any]) -> list[str]:
    """When each thing that happened to this car happened, oldest first.

    The same moments insight.events lists as the car's events, so the ledger
    can tell which of them an alert belongs to.
    """
    stamps = [entry.get(k) for k in ("first_seen", "priced_at", "relisted_at",
                                     "qualified_at", "seller_changed_at",
                                     "photos_at")]
    history = entry.get("price_history") or []
    stamps += [after.get("at") for before, after in zip(history, history[1:])
               if before.get("price") and after.get("price")
               and before["price"] != after["price"]]
    if entry.get("status") == "gone":
        stamps.append(entry.get("removed_at"))
    times = sorted({t for t in (clock.parse(x) for x in stamps) if t})
    return [t.isoformat(timespec="seconds") for t in times]


def _delivery_of(entry: dict[str, Any], at: Any) -> str:
    """What the bot did about the event at ``at``: _delivery for the car's
    newest event, and its record of alert times for an earlier one."""
    when = clock.parse(at)
    later = [t for t in event_times(entry) if when and clock.parse(t) > when]
    if later:
        told = told_about(entry, at, later[0])
        if told:
            return f"delivered at {told}"
        if told == "":
            return "not sent at the time"
    return _delivery(entry)


def _run_at(runs: list[dict[str, Any]], when: str) -> dict[str, Any]:
    """The run that was happening when this was recorded."""
    best: dict[str, Any] = {}
    for run in runs:
        if str(run.get("at") or "") <= str(when or ""):
            if not best or str(run.get("at")) > str(best.get("at")):
                best = run
    keep = ("at", "ok", "listings_seen", "new", "price_drops", "price_rises",
            "removed", "relisted", "qualified", "priced", "requests_made",
            "notified")
    return {k: best[k] for k in keep if k in best}


def scan(state) -> dict[str, Event]:
    """The earliest genuine instance of each kind, from what is already stored."""
    found: dict[str, Event] = {}
    runs = list(state.data.get("runs") or [])

    def offer(event: Event) -> None:
        current = found.get(event.kind)
        if current is None or event.at < current.at:
            found[event.kind] = event

    for lid, entry in state.listings.items():
        title = entry.get("title") or entry.get("display_title") or lid
        url = entry.get("url") or ""

        history = [p for p in (entry.get("price_history") or [])
                   if p.get("price") and p.get("at")]
        # The first move of each kind, not the first move: a car that went up
        # and then down has a drop to offer as well.
        offered: set[str] = set()
        for older, newer in zip(history, history[1:]):
            kind = "price_drop" if newer["price"] < older["price"] else "price_rise"
            if newer["price"] == older["price"] or kind in offered:
                continue
            offered.add(kind)
            delta = newer["price"] - older["price"]
            offer(Event(
                kind=kind, at=newer["at"], listing_id=lid, title=title, url=url,
                before=older["price"], after=newer["price"],
                detail=f"${abs(delta):,} {'off' if delta < 0 else 'more'} "
                       f"(${older['price']:,} to ${newer['price']:,})",
                delivered=_delivery_of(entry, newer["at"]),
                run=_run_at(runs, newer["at"])))
            if len(offered) == 2:
                break

        # A car you stopped watching did not leave the market.
        if entry.get("status") == "gone" and entry.get("removed_at") \
                and not retired_by_owner(entry):
            offer(Event(
                kind="removed", at=entry["removed_at"], listing_id=lid,
                title=title, url=url, before="active", after="gone",
                detail=f"last seen {entry.get('last_seen') or 'unknown'}",
                delivered=_delivery_of(entry, entry["removed_at"]),
                run=_run_at(runs, entry["removed_at"])))

        if entry.get("relisted_at"):
            offer(Event(
                kind="relisted", at=entry["relisted_at"], listing_id=lid,
                title=title, url=url, before="gone", after="active",
                detail="written off, then listed again",
                delivered=_delivery_of(entry, entry["relisted_at"]),
                run=_run_at(runs, entry["relisted_at"])))

        # A call-for-price car naming a figure. Without a priced_at stamp, a
        # price history that starts after first_seen is the evidence.
        priced_at = entry.get("priced_at") or (
            history[0]["at"] if history and entry.get("first_seen")
            and history[0]["at"] > entry["first_seen"] else "")
        if priced_at and history and not entry.get("unpriced"):
            offer(Event(
                kind="priced", at=priced_at, listing_id=lid, title=title,
                url=url, before=None, after=entry.get("price") or history[-1]["price"],
                detail=f"listed with no price on {str(entry.get('first_seen'))[:10]}, "
                       f"then asked ${entry.get('price') or history[-1]['price']:,}",
                delivered=_delivery_of(entry, priced_at),
                run=_run_at(runs, priced_at)))

    return found


def merge(found: dict[str, Event], previous: dict[str, Any]) -> dict[str, Any]:
    """Keep the first sighting of each kind, once recorded, forever."""
    out = dict(previous.get("first", {}) or {})
    for kind, event in found.items():
        fresh = event.to_dict()
        stored = out.get(kind)
        if stored is None or fresh["at"] < stored.get("at", "9999"):
            out[kind] = fresh
        elif (stored.get("listing_id") == fresh["listing_id"]
                and stored.get("at") == fresh["at"]):
            # The same event, read again. When it happened is settled, but
            # the delivery can change (a queued alert is sent, a reconstructed
            # time is recorded properly), so that field is refreshed.
            stored["delivered"] = fresh["delivered"]
    return {"updated_at": clock.now().isoformat(timespec="seconds"),
            "first": out,
            "waiting": [k for k in KINDS if k not in out],
            # Which silence was already reported, so it is announced once
            # rather than hourly.
            "silence_reported": previous.get("silence_reported", ""),
            "coverage_reported": previous.get("coverage_reported", "")}


def render(record: dict[str, Any]) -> str:
    """The ledger as a readable Markdown page."""
    first = record.get("first", {}) or {}
    lines = [
        "# Real market events",
        "",
        "The first time each of these actually happened on autotrader.ca, as",
        "opposed to in a replayed payload. Written by a job that only reads what",
        "the watcher has already stored - it never scrapes and never notifies,",
        "so nothing here can hold up a check.",
        "",
        f"_Last looked: {record.get('updated_at', 'never')}_",
        "",
        "| Event | First seen | Car | Before | After | What was delivered |",
        "|---|---|---|---|---|---|",
    ]
    for kind, label in KINDS.items():
        event = first.get(kind)
        if not event:
            lines.append(f"| {label} | _still waiting_ | | | | |")
            continue
        car = event.get("title") or event.get("listing_id")
        if event.get("url"):
            car = f"[{car}]({event['url']})"
        before, after = event.get("before"), event.get("after")
        fmt = lambda v: (f"${v:,}" if isinstance(v, int) else (v or "—"))
        lines.append(
            f"| {label} | {str(event.get('at'))[:19]} | {car} | "
            f"{fmt(before)} | {fmt(after)} | {event.get('delivered', '')} |")

    for kind, label in KINDS.items():
        event = first.get(kind)
        if not event:
            continue
        lines += ["", f"## {label.capitalize()}", "",
                  f"- **When:** {event.get('at')}",
                  f"- **Car:** {event.get('title')} (`{event.get('listing_id')}`)",
                  f"- **What changed:** {event.get('detail')}",
                  f"- **Delivered:** {event.get('delivered')}"]
        run = event.get("run") or {}
        if run:
            lines.append(
                f"- **The run that saw it:** {run.get('at')} — "
                + ", ".join(f"{k} {v}" for k, v in run.items()
                            if k not in ("at", "notified") and v)
                + (f"; sent via {', '.join(run.get('notified') or []) or 'nothing'}"
                   if "notified" in run else ""))
    return "\n".join(lines) + "\n"


def _succeeded(run: dict[str, Any]) -> bool:
    """Did this run read the site and find nothing wrong but a search down?

    A search that failed to load while others read fine is a narrower watch,
    and the run's own health alert already names it, so the watch has not
    gone silent. Anything else that went wrong (a page read as nothing, the
    bot's own books) still counts against it: those are what "running and
    failing" is for. The runner counts a search as run only when it was
    read, so the two counters never overlap.
    """
    if run.get("skipped"):
        return False
    if run.get("ok"):
        return True
    ran = int(run.get("searches_run") or 0)
    failed = int(run.get("searches_failed") or 0)
    errors = run.get("errors") or []
    return (ran > 0 and failed > 0 and len(errors) <= failed
            and not run.get("invariants"))


def _stopped(stop_file: Path | None) -> bool:
    """Is the budget guard's stop file there? Only asked when a caller names
    one, so a check of the state alone never depends on the working folder."""
    return stop_file is not None and stop_file.exists()


def silence(cfg, state, record: dict[str, Any],
            now: datetime | None = None, *,
            stop_file: Path | None = None) -> dict[str, Any] | None:
    """Has the bot stopped checking?  Returns what to say, or None.

    A watcher cannot report its own absence, so a separate job asks this on
    its own schedule, from the state file alone - and from ``stop_file``, the
    budget guard's file, when the caller names it: while that is there the
    bot stopped itself, which no timer or token will fix.
    """
    hours = float((cfg.get("health", {}) or {}).get("silent_after_hours", 3) or 0)
    if hours <= 0:
        return None
    now = now or clock.now()

    runs = state.data.get("runs") or []
    last_ok = ""
    last_any = ""
    last_error = ""
    for run in runs:
        at = str(run.get("at") or "")
        # A firing that stood down proves the timer is alive but read nothing,
        # and this alarm measures time since the site was last read (as
        # lastCheck does in app.js).
        if _succeeded(run) and at > last_ok:
            last_ok = at
        if at > last_any:
            last_any = at
            errors = run.get("errors") or []
            last_error = str(errors[0]) if errors else ""
    if not last_ok:
        return None            # it has never worked; that is a different alarm

    quiet_for = clock.hours_since(last_ok, now)
    if quiet_for is None or quiet_for < hours:
        return None

    # Report each silence once, not hourly.
    if str(record.get("silence_reported") or "") == last_ok:
        return None
    every = (cfg.get("health", {}) or {}).get("expected_interval_minutes", 30)

    if _stopped(stop_file):
        return {
            "since": last_ok,
            "hours": round(quiet_for, 1),
            "failing": False,
            "budget_stop": True,
            "subject": "AutoTrader watcher is stopped by its budget guard",
            "body": (f"The last successful check was {quiet_for:.1f} hours ago "
                     f"({last_ok}). Nothing is being watched while this is "
                     f"true.\n\nThis is not GitHub's schedule or a token. The "
                     f"bot stopped itself: this month's runner minutes passed "
                     f"the share of the allowance it allows itself, so it "
                     f"wrote {budget.STOP_FILE} on main, and every check "
                     f"stops before it starts while that file is there.\n\n"
                     + budget.RESUME),
        }

    # "Gone quiet" and "running but failing" need different fixes. A run
    # newer than the last success means the bot is still being started.
    running = bool(last_any) and last_any > last_ok
    if running:
        detail = f"\n\nThe most recent one said: {last_error}" if last_error else ""
        return {
            "since": last_ok,
            "hours": round(quiet_for, 1),
            "failing": True,
            "subject": "AutoTrader watcher is running and failing",
            "body": (f"No check has succeeded for {quiet_for:.1f} hours "
                     f"(since {last_ok}), but the bot is still being started - "
                     f"the most recent attempt was at {last_any}.\n\nSo this is "
                     f"not a schedule problem. Something is wrong with the run "
                     f"itself, and the Actions log for the last one will say "
                     f"what.{detail}"),
        }

    # A fixed threshold cannot tell a dead timer from GitHub dropping several
    # windows in a row, and those need opposite fixes. So the alarm reports
    # how much of the recent schedule was served: thin coverage points at the
    # timer; coverage that was healthy until now means something changed.
    from . import insight
    cover = insight.coverage(runs, int(every or 30), now=now,
                             since_change=state.schedule_changed_at)
    served = cover.get("pct_scheduled")
    missed = int(quiet_for * 60 // max(1, int(every or 30)))
    # Runs with no identifiable timer (pushes, hand runs, runs with no
    # recorded trigger) say nothing about the schedule, so without enough
    # attributed runs the alarm does not blame GitHub.
    attributed = sum(n for how, n in (cover.get("by_trigger") or {}).items()
                     if how != "unattributed")
    enough = cover.get("checks", 0) >= 2 and attributed >= 2
    thin = enough and served is not None and served < 70

    if not enough:
        why = (f"That is {words.many(missed, 'missed window')} in a row.\n\n"
               f"This bot cannot tell you whether the schedule is at fault: "
               f"of the checks it has on record, none came from a timer it "
               f"could identify - they were pushes, hand runs, or runs from "
               f"before it started noting how it was started. Check the "
               f"Actions tab for whether the schedule is firing at all.")
    elif thin:
        why = (f"That is {words.many(missed, 'missed window')} in a row, and this "
               f"schedule has been serving only {served}% of the firings asked "
               f"of it. So this is most likely GitHub dropping windows rather "
               f"than anything wrong with the bot - it drops them in runs, not "
               f"one at a time.\n\nThe fix is a timer that does not drop them: "
               f"scripts/keep-time.sh is one, and the README's Schedule section "
               f"sets it up in about five minutes; the bot needs no code change "
               f"to use it.")
    else:
        why = (f"That is {words.many(missed, 'missed window')} in a row, against a "
               f"schedule that had been serving "
               f"{served if served is not None else 'most'}% of its firings - "
               f"so something has changed.\n\nCheck the Actions tab: GitHub "
               f"disables scheduled workflows on repositories with 60 days of "
               f"no activity, and a revoked or expired token stops an external "
               f"timer without announcing it.")

    return {
        "since": last_ok,
        "hours": round(quiet_for, 1),
        "failing": False,
        "served_pct": served,
        "subject": "AutoTrader watcher has gone quiet",
        "body": (f"The last successful check was {quiet_for:.1f} hours ago "
                 f"({last_ok}), and it is supposed to run every "
                 f"{every} minutes. Nothing has been started since, either.\n\n"
                 f"Nothing is being watched while this is true. {why}"),
    }


def marketplace_silence(cfg, state, record: dict[str, Any],
                        now: datetime | None = None) -> dict[str, Any] | None:
    """Has the Marketplace collector stopped reporting, or stopped reading?

    The collector checks in every half hour, reading or not, so no batch for
    hours means the computer is off, asleep, offline or locked out of GitHub.
    Batches that arrive but read nothing mean Facebook is the problem. Each
    silence is reported once. Returns what to say, or None.
    """
    section = state.data.get("marketplace") or {}
    last = section.get("last_batch") or {}
    if not last.get("received"):
        return None            # Marketplace was never set up
    conf = cfg.get("marketplace", {}) or {}
    hours = float(conf.get("silent_after_hours", 2) or 0)
    if hours <= 0:
        return None
    now = now or clock.now()

    heard_for = clock.hours_since(last["received"], now)
    if heard_for is not None and heard_for >= hours:
        key = f"quiet:{last['received']}"
        if record.get("marketplace_reported") == key:
            return None
        return {
            "key": key, "hours": round(heard_for, 1),
            "subject": "Marketplace collector has gone quiet",
            "body": (f"Nothing has come from the Marketplace collector for "
                     f"{heard_for:.1f} hours (the last was from "
                     f"{last.get('host') or 'the collector'} at {last['received']}). "
                     f"It checks in every half hour, reading or not, so the "
                     f"computer is probably asleep, off or offline, or its "
                     f"GitHub token has expired.\n\nOn that computer, run "
                     f"collector/run status to see which. AutoTrader is still "
                     f"being watched."),
        }

    # Checking in, but not reading: the searches themselves are failing.
    read = [str(h.get("last_ok") or "") for h in (section.get("searches") or {}).values()]
    last_read = max(read) if read else ""
    unread_for = clock.hours_since(last_read, now) if last_read else None
    limit = float(conf.get("unread_after_hours", 6) or 0)
    if limit > 0 and unread_for is not None and unread_for >= limit \
            and last.get("polled"):
        key = f"unread:{last_read}"
        if record.get("marketplace_reported") == key:
            return None
        errors = sorted({str(h.get("last_error")) for h in
                         (section.get("searches") or {}).values() if h.get("last_error")})
        return {
            "key": key, "hours": round(unread_for, 1),
            "subject": "Marketplace is not being read",
            "body": (f"The collector is checking in, but no Marketplace search "
                     f"has read successfully for {unread_for:.1f} hours."
                     + (f"\n\nWhat it says: {errors[0]}" if errors else "")
                     + "\n\nOn the collecting computer, run collector/run status."),
        }
    return None


def save(record: dict[str, Any], data_path: Path = DATA_PATH,
         ledger_path: Path = LEDGER_PATH) -> None:
    """Write the ledger back, after something was added to it."""
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(record, indent=1, ensure_ascii=False),
                         encoding="utf-8")
    ledger_path.write_text(render(record), encoding="utf-8")


def update(state, ledger_path: Path = LEDGER_PATH,
           data_path: Path = DATA_PATH) -> dict[str, Any]:
    """Look once, and write down anything seen for the first time."""
    previous: dict[str, Any] = {}
    if data_path.exists():
        try:
            previous = json.loads(data_path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            previous = {}
    record = merge(scan(state), previous)
    # Saved in the file, not just returned: the job reads the file back to
    # decide whether anything happened for the first time.
    record["new_kinds"] = [k for k in record["first"]
                           if k not in (previous.get("first") or {})]
    save(record, data_path, ledger_path)
    return record


def _hours(hours: Any) -> str:
    """A window's length for a subject line: "24 hours", "14.9 hours"."""
    value = float(hours or 0)
    return f"{value:g} hour{'' if value == 1 else 's'}"


def _slot_word(minutes: int) -> str:
    """The schedule's slot as a plural noun, derived from the interval."""
    if minutes == 30:
        return "half-hours"
    if minutes == 60:
        return "hours"
    if minutes % 60 == 0:
        return f"{minutes // 60}-hour slots"
    return f"{minutes}-minute slots"


# Below this share of the expected checks, the bot is not really watching -
# a car can be listed and sold inside a gap this size. Said once per day at
# most, because it is a condition rather than an event.
COVERAGE_FLOOR_PCT = 50.0


def thin_coverage(cfg, state, record: dict[str, Any],
                  now: datetime | None = None, *,
                  stop_file: Path | None = None) -> dict[str, Any] | None:
    """Is the schedule delivering enough checks to be worth trusting?

    Separate from the silence alarm: a watcher that runs reliably but rarely
    is never silent and still misses most of what happens. Says nothing while
    the budget guard's ``stop_file`` is there: the checks stopped because the
    bot stopped them, and the silence alarm says so.
    """
    from . import insight

    health = cfg.get("health", {}) or {}
    floor = float(health.get("coverage_floor_pct", COVERAGE_FLOOR_PCT) or 0)
    if floor <= 0 or _stopped(stop_file):
        return None
    expected = int(health.get("expected_interval_minutes", 30) or 30)
    now = now or clock.now()
    cover = insight.coverage(state.data.get("runs") or [], expected, now=now,
                             since_change=state.schedule_changed_at)
    if cover.get("checks", 0) < 2 or cover["pct"] >= floor:
        return None

    # Once a day: the condition lasts for hours, and an hourly reminder is
    # noise.
    said = str(record.get("coverage_reported") or "")
    if said and said[:10] == now.isoformat()[:10]:
        return None
    # A schedule that changed recently has not had time to be thin.
    if cover.get("partial") and cover.get("window_hours", 0) < 6:
        return None
    # The bot was started often enough, and it is the checks that failed.
    # That is the bot's fault rather than GitHub's, and the silence alarm and
    # the run's own health alert already say so; this one would blame the
    # scheduler for it.
    attempted = cover.get("slots_attempted")
    if attempted is not None and attempted / max(1, cover["expected"]) * 100 >= floor:
        return None

    longest = cover.get("longest_gap_minutes") or 0
    return {
        "pct": cover["pct"],
        # The window the percentage covers, so it can be told apart from a
        # dashboard figure over a different stretch of time.
        "window_hours": cover.get("window_hours"),
        "slots_covered": cover.get("slots_covered"),
        "expected": cover.get("expected"),
        "at": now.isoformat(timespec="seconds"),
        "subject": (f"AutoTrader watcher covered only {cover['pct']}% of the "
                    f"last {_hours(cover['window_hours'])}"),
        # cover["pct"] is the share of slots that had a check, so it is
        # paired with slots_covered rather than the number of runs.
        "body": (f"{cover.get('slots_covered', cover['successful'])} of "
                 f"{cover['expected']} {_slot_word(expected)} in the last "
                 f"{_hours(cover['window_hours'])} had a check, at one asked "
                 f"for every {expected} minutes.\n\n"
                 f"The longest gap was {longest / 60:.1f} hours. A car can be "
                 f"listed and sold inside a gap that size, so treat anything "
                 f"the dashboard says as a sample rather than the market.\n\n"
                 f"This is GitHub's scheduler rather than the bot: check the "
                 f"Actions tab to see how many runs were actually served."),
    }
