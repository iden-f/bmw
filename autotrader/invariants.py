"""Checks that the bot's own bookkeeping still makes sense.

The failures that matter here are not crashes but silently wrong state: a car
reported gone while still listed, a working search reporting zero listings, a
car stored and never mentioned. Each rule states something that must be true
of stored state after every cycle; a violation fails the run and writes the
evidence out, rather than logging a warning nobody reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from . import clock
from .state import HIDDEN_REASON_PREFIX

REPORT_PATH = Path("diagnostics/invariants.json")

# A car needs this many consecutive absences before it counts as gone, so an
# active car carrying more than that means the countdown stopped working.
GRACE_RUNS = 2
# Ids listed in the failure message. Enough to debug from, not a dump.
MAX_EXAMPLES = 8
# Quiet hours end and outages clear; an alert queued longer than this is lost,
# not waiting.
STUCK_QUEUE_HOURS = 12


@dataclass
class Violation:
    rule: str
    detail: str
    examples: list[str] = field(default_factory=list)
    count: int = 0

    def __str__(self) -> str:
        shown = ", ".join(self.examples[:MAX_EXAMPLES])
        more = f" (+{self.count - len(self.examples[:MAX_EXAMPLES])} more)" \
            if self.count > MAX_EXAMPLES else ""
        return f"{self.rule}: {self.detail} [{shown}{more}]" if shown \
            else f"{self.rule}: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "detail": self.detail,
                "count": self.count, "examples": self.examples[:MAX_EXAMPLES * 4]}


def _violation(rule: str, detail: str, ids: list[str]) -> Violation:
    return Violation(rule, detail, list(ids)[:MAX_EXAMPLES * 4], len(ids))


def check(cfg, state, report=None, payload: dict[str, Any] | None = None,
          seen: set[str] | None = None) -> list[Violation]:
    """Everything that must hold once a run has finished writing state.

    ``seen`` is the set of listing ids this run actually read. Without it the
    count checks compare against every car in state, including ones this run
    never looked at, which is a difference rather than a fault.
    """
    out: list[Violation] = []
    listings = state.listings
    configured = {s.id for s in cfg.searches}
    active = {lid: e for lid, e in listings.items()
              if e.get("status") == "active"}

    # ---- ownership -------------------------------------------------
    # A car belongs to exactly one search; otherwise a working watch can
    # report zero cars, or have another watch's rules applied to its listings.
    unowned = [lid for lid, e in active.items() if not e.get("search_id")]
    if unowned:
        out.append(_violation(
            "one-owner", "active listings with no search to their name", unowned))

    orphans = [lid for lid, e in active.items()
               if e.get("search_id") and e["search_id"] not in configured]
    if orphans:
        out.append(_violation(
            "one-owner", "active listings owned by a search that is not "
            "configured any more", orphans))

    # ---- every car is accounted for --------------------------------
    # Delivered, owed, or deliberately quiet with a reason: a car that matters
    # must never go unmentioned.
    unaccounted = [
        lid for lid, e in listings.items()
        if not e.get("notified_at") and not e.get("pending")
        and not e.get("quiet_reason")
    ]
    if unaccounted:
        out.append(_violation(
            "accounted-for", "listings that were neither delivered, queued, "
            "nor deliberately silenced with a reason", unaccounted))

    contradictory = [lid for lid, e in listings.items()
                     if e.get("pending") and e.get("notified")]
    if contradictory:
        out.append(_violation(
            "accounted-for", "listings queued for delivery and marked "
            "delivered at the same time", contradictory))

    # A queue that only grows means nothing is getting through, so the run
    # says so rather than counting those alerts as handled.
    cutoff = (clock.now()
              - timedelta(hours=STUCK_QUEUE_HOURS)).isoformat(timespec="seconds")
    stranded = [lid for lid, e in listings.items()
                if isinstance(e.get("pending"), dict)
                and str(e["pending"].get("since") or "") < cutoff]
    if stranded:
        out.append(_violation(
            "accounted-for", f"alerts queued for more than "
            f"{STUCK_QUEUE_HOURS} hours and still not delivered", stranded))

    # ---- hiding a car is a decision, so it has a reason -------------
    unexplained = [lid for lid, e in listings.items()
                   if e.get("filtered") and not e.get("filter_reason")]
    if unexplained:
        out.append(_violation(
            "hidden-has-a-reason", "listings hidden by a rule that does not "
            "say which rule", unexplained))

    stale = [lid for lid, e in listings.items()
             if not e.get("filtered")
             and str(e.get("quiet_reason") or "").startswith(HIDDEN_REASON_PREFIX)]
    if stale:
        out.append(_violation(
            "hidden-has-a-reason", "listings that are not hidden but still say "
            "a rule is why they were kept quiet", stale))

    # ---- states that cannot both be true ---------------------------
    resurrected = [lid for lid, e in listings.items()
                   if e.get("status") == "active" and e.get("removed_at")]
    if resurrected:
        out.append(_violation(
            "not-both", "listings that are active and carry a removal time",
            resurrected))

    mislabelled = [lid for lid, e in listings.items()
                   if bool(e.get("unpriced")) != (e.get("price") is None)]
    if mislabelled:
        out.append(_violation(
            "not-both", "listings whose call-for-price flag disagrees with "
            "whether they have a price", mislabelled))

    vanished = [lid for lid, e in listings.items()
                if e.get("status") == "gone" and not e.get("removed_at")]
    if vanished:
        out.append(_violation(
            "not-both", "listings marked gone with no record of when",
            vanished))

    # A stored price that disagrees with its own history means one of the two
    # was written without the other, which is how a phantom price drop starts.
    contradicted = []
    for lid, e in listings.items():
        history = e.get("price_history") or []
        if e.get("price") is None or not history:
            continue
        if history[-1].get("price") != e.get("price"):
            contradicted.append(lid)
    if contradicted:
        out.append(_violation(
            "not-both", "listings whose stored price is not the last entry in "
            "their own price history", contradicted))

    backwards = [lid for lid, e in listings.items()
                 if e.get("first_seen") and e.get("last_seen")
                 and str(e["last_seen"]) < str(e["first_seen"])]
    if backwards:
        out.append(_violation(
            "not-both", "listings last seen before they were first seen",
            backwards))

    stuck = [lid for lid, e in active.items()
             if int(e.get("misses", 0) or 0) > GRACE_RUNS]
    if stuck:
        out.append(_violation(
            "grace-bounded", f"active listings absent for more than "
            f"{GRACE_RUNS} runs without being resolved either way", stuck))

    # ---- the numbers agree with each other -------------------------
    if report is not None and getattr(report, "filtered_out", None) is not None:
        # Like against like: what this run hid, against the cars it read. A
        # run with a failed search makes no claim about that search's cars.
        pool = active if seen is None else {
            lid: e for lid, e in active.items() if lid in seen}
        hidden = sum(1 for e in pool.values() if e.get("filtered"))
        if (report.searches_run and report.searches_run == len(configured)
                and not report.searches_failed and not report.empty_parses
                and report.filtered_out != hidden):
            out.append(Violation(
                "counts-reconcile",
                f"the run reported {report.filtered_out} listing(s) hidden by "
                f"your rules; state holds {hidden} among the cars it read"))

    if payload is not None:
        # The page's counts describe state; its listings are a capped slice of
        # it. So the counts are checked against state, not against a recount
        # of the published list (the same arithmetic on both sides, which can
        # never fail), and the cap has its own rules below.
        counts = (payload.get("health") or {}).get("counts") or {}
        shown = sum(1 for e in active.values() if not e.get("filtered"))
        hidden_now = sum(1 for e in active.values() if e.get("filtered"))
        published = sum(1 for l in payload.get("listings", [])
                        if l.get("status") == "active" and not l.get("filtered"))
        published_hidden = sum(1 for l in payload.get("listings", [])
                               if l.get("status") == "active" and l.get("filtered"))
        if counts.get("active") != shown:
            out.append(Violation(
                "counts-reconcile",
                f"the dashboard says {counts.get('active')} live listing(s) "
                f"where state holds {shown}"))
        if counts.get("filtered") != hidden_now:
            out.append(Violation(
                "counts-reconcile",
                f"the dashboard says {counts.get('filtered')} hidden listing(s) "
                f"where state holds {hidden_now}"))
        # A car hidden by a rule is still on the site and still tracked, but it
        # must never be counted or listed as one you are watching.
        leaked = [l.get("id") for l in payload.get("listings", [])
                  if l.get("filtered") and not l.get("filter_reason")]
        if leaked:
            out.append(_violation(
                "not-both", "hidden listings published without the rule that "
                "hid them", [str(i) for i in leaked]))

        # The published file is capped, so it may legitimately hold fewer than
        # state does - never more.
        if published > shown:
            out.append(Violation(
                "counts-reconcile",
                f"the dashboard publishes {published} live listing(s) from a "
                f"state that only has {shown}"))
        if published_hidden > hidden_now:
            out.append(Violation(
                "counts-reconcile",
                f"the dashboard publishes {published_hidden} hidden listing(s) "
                f"from a state that only has {hidden_now}"))
        # When it holds fewer, it must say how many it left out, because the
        # counts describe the larger number.
        left_out = (payload.get("health") or {}).get("left_out")
        missing = (shown + hidden_now) - (published + published_hidden)
        if missing > 0 and int(left_out or 0) <= 0:
            out.append(Violation(
                "counts-reconcile",
                f"the dashboard leaves {missing} listing(s) out of the file "
                f"and does not say so"))

    return out


def write(violations: list[Violation], cfg=None, state=None,
          path: Path = REPORT_PATH) -> Path | None:
    """Keep the evidence, so a violation can be fixed rather than guessed at."""
    if not violations:
        return None
    payload = {
        "at": clock.now().isoformat(timespec="seconds"),
        "violations": [v.to_dict() for v in violations],
    }
    if state is not None:
        # The offending entries themselves: the first thing anyone asks for,
        # and state.json is too big to search by hand.
        ids = {lid for v in violations for lid in v.examples}
        payload["entries"] = {lid: state.listings.get(lid)
                              for lid in list(ids)[:40]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False,
                                   default=str), encoding="utf-8")
    except OSError:
        return None
    return path
