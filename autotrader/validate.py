"""Judging whether a parse can be trusted.

A parse that looks wrong stops the run before anything is recorded, rather
than filling state with bad data that has to be unpicked later. The first run
against the live site is where this matters most.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .listing import Listing

# Below this many listings the ratios below are meaningless - a search with two
# results genuinely can have neither of them priced.
MIN_SAMPLE = 5


@dataclass
class Assessment:
    search_name: str
    strategy: str
    count: int
    concerns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    sample: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return not self.concerns

    def to_dict(self) -> dict[str, Any]:
        return {"search": self.search_name, "strategy": self.strategy,
                "count": self.count, "concerns": self.concerns,
                "notes": self.notes, "sample": self.sample}


def _sample(listings: list[Listing], limit: int = 3) -> list[dict[str, Any]]:
    out = []
    for listing in listings[:limit]:
        out.append({
            "id": listing.id,
            "title": listing.display_title,
            "price": listing.price_text,
            "price_source": listing.price_source or "none",
            "odometer": listing.mileage_text,
            "year": listing.year,
            "where": ", ".join(x for x in (listing.location, listing.province) if x),
            "photos": len(listing.images),
            "url": listing.url,
        })
    return out


def assess(listings: list[Listing], strategy: str, search_name: str = "",
           *, said_no_results: bool = False, candidates: dict[str, int] | None = None
           ) -> Assessment:
    """Decide whether this parse looks like real listings or like debris."""
    result = Assessment(search_name=search_name, strategy=strategy,
                        count=len(listings), sample=_sample(listings))

    if not listings:
        if said_no_results:
            result.notes.append("The site says this search matches nothing. "
                                "That is a narrow search, not a broken parser.")
        else:
            result.concerns.append(
                "No listings were read from a page that loaded successfully, and "
                "the site did not say the search was empty. Every parser strategy "
                "has probably fallen behind AutoTrader's markup.")
        return result

    if candidates:
        working = [name for name, count in candidates.items() if count]
        if len(working) == 1:
            result.notes.append(
                f"Only the '{working[0]}' strategy found anything. It works, but "
                f"there is no fallback left if AutoTrader changes again.")

    if strategy == "regex":
        # The last-resort strategy recovers links and nothing else.
        result.concerns.append(
            "Only the raw-regex fallback could read this page, which recovers "
            "listing links but no prices, odometers or titles. The page markup "
            "has almost certainly changed.")

    total = len(listings)
    priced = sum(1 for l in listings if l.price is not None)
    with_km = sum(1 for l in listings if l.mileage_km is not None)
    titled = sum(1 for l in listings
                 if l.display_title and not l.display_title.startswith("AutoTrader listing"))
    located = sum(1 for l in listings if l.location or l.province)
    dated = sum(1 for l in listings if l.year)

    if total >= MIN_SAMPLE:
        # "Call for price" is common; almost none priced is not.
        if priced / total < 0.2:
            result.concerns.append(
                f"Only {priced} of {total} listings have a price. Some listings "
                f"genuinely say 'call for price', but not nine in ten.")
        elif priced / total < 0.6:
            result.notes.append(f"{total - priced} of {total} listings have no price.")

        if with_km / total < 0.2:
            result.concerns.append(
                f"Only {with_km} of {total} listings have an odometer reading.")
        elif with_km / total < 0.6:
            result.notes.append(f"{total - with_km} of {total} have no odometer reading.")

        # AutoTrader publishes the model year for every car it lists, so its
        # absence is a parser problem rather than a gap in the data.
        if dated == 0:
            result.concerns.append(
                f"None of the {total} listings has a model year. The site does "
                f"publish one, so the parser is looking in the wrong place.")
        elif dated / total < 0.5:
            result.notes.append(f"{total - dated} of {total} have no model year.")

        if titled / total < 0.5:
            result.concerns.append(
                f"Only {titled} of {total} listings have a readable name - the rest "
                f"fell back to 'AutoTrader listing <id>'.")

    if located == 0 and total >= MIN_SAMPLE:
        result.notes.append("No listing had a location, which is unusual.")

    # Prices that are obviously not car prices mean we matched the wrong number.
    prices = [l.price for l in listings if l.price is not None]
    if prices:
        absurd = [p for p in prices if p < 500 or p > 2_000_000]
        if absurd:
            result.concerns.append(
                f"{len(absurd)} listing(s) have a price outside any plausible range "
                f"(e.g. ${absurd[0]:,}). The parser is probably reading the wrong number.")

    mileages = [l.mileage_km for l in listings if l.mileage_km is not None]
    if mileages:
        absurd_km = [m for m in mileages if m > 1_500_000]
        if absurd_km:
            result.concerns.append(
                f"{len(absurd_km)} listing(s) have an implausible odometer reading "
                f"(e.g. {absurd_km[0]:,} km).")

    return result


def report_markdown(assessments: list[Assessment], *, ok: bool) -> str:
    """A human-readable report, kept as validation-report.md in the vault."""
    lines = ["# First-run parse check", ""]
    if ok:
        lines.append("**Looks right.** The parser read the site the way it should.")
    else:
        lines.append("**Something looks wrong.** Nothing was recorded, so no bad data "
                     "was saved. Details below.")
    lines.append("")

    for item in assessments:
        lines.append(f"## {item.search_name or 'Search'}")
        lines.append("")
        lines.append(f"- Strategy used: `{item.strategy}`")
        lines.append(f"- Listings read: **{item.count}**")
        for concern in item.concerns:
            lines.append(f"- ⚠️ **{concern}**")
        for note in item.notes:
            lines.append(f"- {note}")
        lines.append("")
        if item.sample:
            lines.append("### Sample — compare these against autotrader.ca by eye")
            lines.append("")
            lines.append("| | Price | Odometer | Where | Photos |")
            lines.append("|---|---|---|---|---|")
            for row in item.sample:
                lines.append(
                    f"| [{row['title']}]({row['url']}) | {row['price']} "
                    f"({row['price_source']}) | {row['odometer']} | "
                    f"{row['where'] or '—'} | {row['photos']} |")
            lines.append("")
    return "\n".join(lines)


def report_text(assessments: list[Assessment], *, ok: bool) -> str:
    """The same thing for a notification, where markdown tables do not render."""
    lines = []
    lines.append("Your AutoTrader watcher checked itself on its first run.")
    lines.append("")
    for item in assessments:
        lines.append(f"{item.search_name}: {item.count} listing(s) via '{item.strategy}'")
        for concern in item.concerns:
            lines.append(f"  PROBLEM: {concern}")
        for note in item.notes:
            lines.append(f"  note: {note}")
        for row in item.sample:
            lines.append(f"  - {row['title']} | {row['price']} | {row['odometer']} "
                         f"| {row['where'] or 'unknown'}")
        lines.append("")
    if ok:
        lines.append("Everything looks right. Nothing more to do - it will keep watching.")
    else:
        lines.append("Nothing was saved, so no bad data got in. Run "
                     "'python -m autotrader doctor --live' to look closer.")
    return "\n".join(lines).strip()
