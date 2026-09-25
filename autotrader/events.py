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

from . import clock, words

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
        for older, newer in zip(history, history[1:]):
            kind = "price_drop" if newer["price"] < older["price"] else "price_rise"
            if newer["price"] == older["price"]:
                continue
            delta = newer["price"] - older["price"]
            offer(Event(
                kind=kind, at=newer["at"], listing_id=lid, title=title, url=url,
                before=older["price"], after=newer["price"],
                detail=f"${abs(delta):,} {'off' if delta < 0 else 'more'} "
                       f"(${older['price']:,} to ${newer['price']:,})",
                delivered=_delivery(entry), run=_run_at(runs, newer["at"])))
            break

        if entry.get("status") == "gone" and entry.get("removed_at"):
            offer(Event(
                kind="removed", at=entry["removed_at"], listing_id=lid,
                title=title, url=url, before="active", after="gone",
                detail=f"last seen {entry.get('last_seen') or 'unknown'}",
                delivered=_delivery(entry), run=_run_at(runs, entry["removed_at"])))

        if entry.get("relisted_at"):
            offer(Event(
                kind="relisted", at=entry["relisted_at"], listing_id=lid,
                title=title, url=url, before="gone", after="active",
                detail="written off, then listed again",
                delivered=_delivery(entry), run=_run_at(runs, entry["relisted_at"])))

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
                delivered=_delivery(entry), run=_run_at(runs, priced_at)))

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


def silence(cfg, state, record: dict[str, Any],
            now: datetime | None = None) -> dict[str, Any] | None:
    """Has the bot stopped checking?  Returns what to say, or None.

    A watcher cannot report its own absence, so a separate job asks this on
    its own schedule, from the state file alone.
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
        if run.get("ok") and not run.get("skipped") and at > last_ok:
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
                  now: datetime | None = None) -> dict[str, Any] | None:
    """Is the schedule delivering enough checks to be worth trusting?

    Separate from the silence alarm: a watcher that runs reliably but rarely
    is never silent and still misses most of what happens.
    """
    from . import insight

    health = cfg.get("health", {}) or {}
    floor = float(health.get("coverage_floor_pct", COVERAGE_FLOOR_PCT) or 0)
    if floor <= 0:
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

    longest = cover.get("longest_gap_minutes") or 0
    return {
        "pct": cover["pct"],
        # The window the percentage covers, so it can be told apart from a
        # dashboard figure over a different stretch of time.
        "window_hours": cover.get("window_hours"),
        "slots_covered": cover.get("slots_covered"),
        "expected": cover.get("expected"),
        "at": now.isoformat(timespec="seconds"),
        "subject": f"AutoTrader watcher covered only {cover['pct']}% of yesterday",
        # cover["pct"] is the share of slots that had a check, so it is
        # paired with slots_covered rather than the number of runs.
        "body": (f"{cover.get('slots_covered', cover['successful'])} of "
                 f"{cover['expected']} {_slot_word(expected)} in the last "
                 f"{cover['window_hours']} hours had a check, at one asked "
                 f"for every {expected} minutes.\n\n"
                 f"The longest gap was {longest / 60:.1f} hours. A car can be "
                 f"listed and sold inside a gap that size, so treat anything "
                 f"the dashboard says as a sample rather than the market.\n\n"
                 f"This is GitHub's scheduler rather than the bot: check the "
                 f"Actions tab to see how many runs were actually served."),
    }
