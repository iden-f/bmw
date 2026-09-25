"""Rendering listings into the shapes each notification channel wants."""

from __future__ import annotations

import html as htmllib
import re
from typing import Any

from . import clock
from .state import Change

MAX_SMS = 1500


def _esc(text: Any) -> str:
    return htmllib.escape(str(text or ""), quote=True)


def _facts(listing) -> list[str]:
    """The short chips shown under a car's name."""
    out = [listing.price_text]
    if listing.mileage_km is not None:
        out.append(listing.mileage_text)
    title = listing.display_title.lower()
    # short_trim, not trim: the raw trim is often a dealer's option list.
    for value in (listing.short_trim, listing.drivetrain, listing.transmission,
                  listing.color, listing.location or listing.province):
        text = str(value or "").strip()
        # Placeholders such as "n/a" say nothing and are not worth a chip.
        if not text or text.lower() in {"n/a", "na", "-", "unknown", "none"}:
            continue
        # Skip what display_title already says, such as the trim.
        if text.lower() not in title:
            out.append(text)
    return out


def _short(listing) -> str:
    """The car, in as few words as still identify it on a lock screen.

    A long title is cut at a word boundary and ends with an ellipsis, so it
    never stops mid-word or on a dangling space.
    """
    title = str(getattr(listing, "display_title", "") or "").split("|")[0].strip()
    title = title or "listing"
    if len(title) <= 48:
        return title
    head, space, _ = title[:48].rpartition(" ")
    cut = (head if space and len(head) >= 16 else title[:48]).rstrip(" ,-*/|")
    return f"{cut}\u2026"


def _one_line(change: Change) -> str:
    """A single change, with the figure in it: the number is the message."""
    listing = change.listing
    car = _short(listing)
    if change.kind == Change.PRICE_DROP:
        return (f"{car} down ${abs(change.delta or 0):,} "
                f"to {listing.price_text}")
    if change.kind == Change.PRICE_RISE:
        return (f"{car} up ${abs(change.delta or 0):,} "
                f"to {listing.price_text}")
    if change.kind == Change.PRICED:
        return f"{car} now {listing.price_text} (was call for price)"
    if change.kind == Change.REMOVED:
        return f"{car} gone from the site"
    if change.kind == Change.RELISTED:
        if change.delta:
            way = "cheaper" if change.delta < 0 else "dearer"
            return (f"{car} back on the market ${abs(change.delta):,} {way}, "
                    f"at {listing.price_text}")
        return f"{car} back on the market at {listing.price_text}"
    if change.kind == Change.QUALIFIED:
        return f"{car} is back inside your rules at {listing.price_text}"
    if change.kind == Change.NEW:
        # Needs its own verb: with a single change nothing else supplies one.
        return f"{car} just listed at {listing.price_text}"
    return f"{car} {listing.price_text}"


def headline(changes: list[Change]) -> str:
    """One line summarising a run, used as the subject / message title.

    One change is named with its figure. Several lead with the biggest price
    drop, if any, and count the rest, so the drop is not buried in a tally.
    """
    if not changes:
        return "AutoTrader: no changes"
    if len(changes) == 1:
        return "AutoTrader: " + _one_line(changes[0])

    counts = {kind: sum(1 for c in changes if c.kind == kind)
              for kind in (Change.PRICE_DROP, Change.QUALIFIED, Change.NEW,
                           Change.PRICED, Change.RELISTED, Change.PRICE_RISE,
                           Change.REMOVED)}
    drops = [c for c in changes if c.kind == Change.PRICE_DROP]
    if drops:
        best = min(drops, key=lambda c: c.delta or 0)
        lead = f"{_short(best.listing)} down ${abs(best.delta or 0):,}"
        rest = len(changes) - 1
        return f"AutoTrader: {lead}" + (f", +{rest} more change{'s' if rest != 1 else ''}" if rest else "")

    label = {
        # Before "new" on purpose: a car coming back inside your rules is
        # announced once, while a new listing will still be there tomorrow.
        Change.QUALIFIED: ("back inside your rules", "back inside your rules"),
        Change.NEW: ("new listing", "new listings"),
        Change.PRICED: ("price published", "prices published"),
        Change.RELISTED: ("back on the market", "back on the market"),
        Change.PRICE_RISE: ("price increase", "price increases"),
        Change.REMOVED: ("removed", "removed"),
    }
    bits = []
    for kind, (one, many) in label.items():
        n = counts.get(kind, 0)
        if n:
            bits.append(f"{n} {one if n == 1 else many}")
    return "AutoTrader: " + ", ".join(bits)


#: The mark each kind of change wears where a channel can render one.
_MARKS = {
    Change.PRICE_DROP: "\u2193",      # down arrow
    Change.PRICE_RISE: "\u2191",
    Change.PRICED: "\U0001f4b2",      # heavy dollar sign
    Change.REMOVED: "\u2716",
    Change.RELISTED: "\u21ba",        # anticlockwise open circle arrow
    Change.QUALIFIED: "\u2713",
}


def _change_words(change: Change) -> str:
    """What this change is, in words. One definition for every channel.

    Shared so that no channel words a kind differently or leaves one
    unlabelled, which would make it read as a new listing.
    """
    if change.kind == Change.PRICE_DROP:
        return f"PRICE DROP ${abs(change.delta or 0):,} off"
    if change.kind == Change.PRICE_RISE:
        return f"Price up ${abs(change.delta or 0):,}"
    if change.kind == Change.PRICED:
        return "Price now shown"
    if change.kind == Change.REMOVED:
        return "Removed"
    if change.kind == Change.RELISTED:
        if change.delta:
            way = "cheaper" if change.delta < 0 else "dearer"
            return f"Back on the market, ${abs(change.delta):,} {way}"
        return "Back on the market"
    if change.kind == Change.QUALIFIED:
        return "Back inside your rules"
    return ""


#: Below this, saying how old an alert is tells you nothing you would act on.
HELD_WORTH_SAYING_MINUTES = 60


def _held_note(change: Change) -> str:
    """"held 9 hours" when this alert waited, empty when it did not.

    An alert held back by quiet hours or a failed delivery is sent by a later
    run; saying how long it waited keeps it from reading as fresh news.
    """
    from .insight import _span
    held = clock.hours_since(getattr(change, "held_since", None))
    if held is None or held * 60 < HELD_WORTH_SAYING_MINUTES:
        return ""
    return f"held {_span(held)}"


#: The colour each kind of change wears in the email. Colours only: the words
#: come from _change_words, so every channel names a kind the same way.
_BADGE_COLOURS = {
    Change.NEW: "#1d4ed8",
    Change.PRICE_DROP: "#0f7b3f",
    Change.PRICE_RISE: "#9a3412",
    Change.PRICED: "#0f7b3f",
    Change.REMOVED: "#6b7280",
    Change.RELISTED: "#6d28d9",
    Change.QUALIFIED: "#0369a1",
}
_BADGE_FALLBACK = "#374151"


def _badge_words(change: Change) -> str:
    """The upper-case badge label, derived from _change_words.

    Derived rather than listed again, so every kind has a badge.
    """
    words = _change_words(change)
    return (words or "NEW").upper()


def _badge_html(change: Change) -> str:
    colour = _BADGE_COLOURS.get(change.kind, _BADGE_FALLBACK)
    label = _esc(_badge_words(change))
    held = _held_note(change)
    aside = (f'<span style="display:inline-block;margin-left:6px;color:#6b7280;'
             f'font-size:12px;">{_esc(held)}</span>') if held else ""
    return (f'<span style="display:inline-block;background:{colour};color:#ffffff;'
            f'font-size:12px;font-weight:700;padding:3px 8px;border-radius:99px;">'
            f'{label}</span>{aside}')


def _parts(change: Change) -> list[str]:
    """The label pieces for one change, in order, any of them possibly absent."""
    return [p for p in (_change_words(change), _held_note(change)) if p]


def _change_prefix(change: Change) -> str:
    parts = _parts(change)
    return (", ".join(parts) + " - ") if parts else ""


#: The same words the dashboard's cards use for the same ratio.
PER_KM = "per 1,000 km"


def the_move(change: Change) -> str:
    """Both figures of a price move, or "" when there is none.

    Includes the old price so the reader need not remember it, worded as on
    the dashboard's cards.
    """
    if change.old_price is None or change.new_price is None:
        return ""
    if change.kind not in (Change.PRICE_DROP, Change.PRICE_RISE,
                           Change.RELISTED) or not change.delta:
        return ""
    pct = abs(change.delta_pct or 0.0)
    way = "down" if (change.delta or 0) < 0 else "up"
    return (f"${change.old_price:,} to ${change.new_price:,}, "
            f"{way} ${abs(change.delta):,} ({pct:.1f}%)")


def the_ratio(listing) -> str:
    """"$370 per 1,000 km", or "" when the car has no price or no odometer.

    Worded as on the dashboard's cards. It is the most useful number for
    telling two cars of the same model apart.
    """
    from . import insight
    ratio = insight.per_1000km(listing.price, listing.mileage_km)
    return f"${int(round(ratio)):,} {PER_KM}" if ratio else ""


def how_far_it_moved(change: Change) -> str:
    """"down $700, 1.5%" - for channels that already show both prices."""
    if not change.delta or change.delta_pct is None:
        return ""
    way = "down" if change.delta < 0 else "up"
    return f"{way} ${abs(change.delta):,}, {abs(change.delta_pct):.1f}%"


def worth_knowing(change: Change) -> list[str]:
    """The extra facts a lock screen has room for, in order of usefulness.

    Kept out of `_facts` because SMS shares it and is billed per segment;
    these go only to the free channels.
    """
    return [part for part in (the_move(change), the_ratio(change.listing)) if part]


def as_text(changes: list[Change], *, limit: int = 12, footer: str = "") -> str:
    """Plain text, for SMS and any channel without formatting."""
    lines = [headline(changes), ""]
    for change in changes[:limit]:
        listing = change.listing
        lines.append(f"{_change_prefix(change)}{listing.display_title}")
        lines.append("  " + " | ".join(_facts(listing)))
        extra = worth_knowing(change)
        if extra:
            lines.append("  " + " | ".join(extra))
        if listing.url:
            lines.append("  " + listing.url)
        lines.append("")
    if len(changes) > limit:
        lines.append(f"...and {len(changes) - limit} more.")
    if footer:
        lines.append(footer)
    return "\n".join(lines).strip()


# ------------------------------------------------------------------ the phone
#
# Push notifications, written for a lock screen rather than an inbox: the
# heading names the car with its price, location and kilometres; new listings
# come first, price drops second, and small drops ride along behind a new car
# instead of alerting on their own.


def in_order(changes: list[Change]) -> list[Change]:
    """New cars first, price drops second, everything else after.

    Every channel and the digest cap use this order, so the cars a long
    message leaves out are the least important. New cars keep detection
    order; drops go biggest first, with riding drops behind the rest. A
    shortlisted car leads its own section, never ahead of an unseen car.
    """
    def key(item):
        index, change = item
        if change.kind in Change.NEW_TO_YOU:
            group = 0
        elif change.kind == Change.PRICE_DROP:
            group = 1
        elif change.kind == Change.PRICED:
            group = 2
        else:
            group = 3
        mine = 0 if getattr(change.listing, "shortlisted", False) else 1
        size = -abs(change.delta or 0) if change.kind == Change.PRICE_DROP else 0
        return (group, 1 if getattr(change, "rider", False) else 0, mine, size, index)
    return [c for _, c in sorted(enumerate(changes), key=key)]


# Title words that change what the car is, with their canonical spelling.
# Anything else a dealer puts in the title is left for the ad to say.
_VARIANTS = (
    (r"\bcsl\b", "CSL"),
    (r"\bcs\b", "CS"),
    (r"\bgts\b", "GTS"),
    (r"\bcompetition\b|\bcomp\b", "Competition"),
    (r"\bmanual\b|\b6mt\b|\b6[- ]?speed\b|\bstick\b", "Manual"),
    (r"\bconvertible\b|\bcabrio(let)?\b", "Convertible"),
    (r"\btouring\b", "Touring"),
)
_REBUILT = re.compile(r"\b(rebuilt|salvage[d]?)\b", re.I)


def car_name(listing) -> str:
    """The car, not the ad: year, make, model and any variant that matters.

    Only the variant words in _VARIANTS are added. The dealer's own title is
    the wrong thing to lead a notification with, since it often carries
    colours, options and sales copy.
    """
    base = " ".join(str(part) for part in (getattr(listing, "year", None),
                                           getattr(listing, "make", ""),
                                           getattr(listing, "model", ""))
                    if part)
    if not getattr(listing, "model", "") or not base:
        return _short(listing)
    text = " ".join(str(getattr(listing, f, "") or "")
                    for f in ("title", "trim")).lower()
    extras: list[str] = []
    for pattern, label in _VARIANTS:
        if label in extras or (label == "CS" and "CSL" in extras):
            continue
        if re.search(pattern, text) and label.lower() not in base.lower():
            extras.append(label)
    return " ".join([base, *extras])


def short_link(listing) -> str:
    """The ad by its id alone, far shorter than the full address.

    autotrader.ca redirects /offers/<id> to the full address. The short link
    is also sturdier: the long one carries a slug made from the dealer's
    title, which changes whenever they edit it.
    """
    lid = str(getattr(listing, "id", "") or "")
    url = str(getattr(listing, "url", "") or "")
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lid) \
            and "autotrader.ca/offers/" in url:
        return f"https://www.autotrader.ca/offers/{lid}"
    return url


def _where(listing) -> str:
    return str(getattr(listing, "location", "") or getattr(listing, "province", "") or "")


def _push_facts(listing) -> str:
    """Kilometres, location, colour and a rebuilt title: what the name omits."""
    bits = []
    if getattr(listing, "mileage_km", None) is not None:
        bits.append(listing.mileage_text)
    if _where(listing):
        bits.append(_where(listing))
    colour = str(getattr(listing, "color", "") or "").strip()
    if colour and colour.lower() not in {"n/a", "na", "-", "unknown", "none"}:
        bits.append(colour)
    # The one thing in a dealer's free text worth carrying to the phone.
    blob = " ".join(str(getattr(listing, f, "") or "") for f in ("title", "trim"))
    if _REBUILT.search(blob):
        bits.append("REBUILT TITLE")
    return " · ".join(bits)


def _dropped(change: Change) -> str:
    """The drop from the last reported price, as "down $X from $Y"."""
    if change.old_price is None or change.new_price is None:
        return ""
    return f"down ${abs(change.delta or 0):,} from ${change.old_price:,}"


def push_title(changes: list[Change]) -> str:
    """The heading: the car it is about, its price, where it is, its km.

    The lead is the first change in render order, so a new car whenever
    there is one. The rest are only counted: the heading is for deciding
    whether to open the notification, and the body says what they are.
    """
    ordered = in_order(changes)
    if not ordered:
        return "No changes"
    lead = ordered[0]
    car = lead.listing
    name = car_name(car)
    where, km = _where(car), (car.mileage_text if car.mileage_km is not None else "")
    tail = [part for part in (where, km) if part]
    if lead.kind == Change.NEW:
        parts = [f"New {name}", car.price_text, *tail]
    elif lead.kind == Change.QUALIFIED:
        parts = [f"Now in your rules: {name}", car.price_text, *tail]
    elif lead.kind == Change.RELISTED:
        parts = [f"Back on sale: {name}", car.price_text, *tail]
    elif lead.kind == Change.PRICE_DROP:
        parts = [f"{name} down ${abs(lead.delta or 0):,}",
                 f"now {car.price_text}", *tail]
    elif lead.kind == Change.PRICED:
        parts = [f"{name} priced", car.price_text, *tail]
    else:
        return headline(ordered).removeprefix("AutoTrader: ")
    title = " · ".join(parts)
    # Count the rest by kind: "(+1 price drop)" says whether it is worth
    # expanding, where "(+1 more)" would not.
    rest = ordered[1:]
    if not rest:
        return title
    new = sum(1 for c in rest if c.kind in Change.NEW_TO_YOU)
    drops = sum(1 for c in rest if c.kind == Change.PRICE_DROP)
    other = len(rest) - new - drops
    bits = []
    if new:
        bits.append(f"+{new} new")
    if drops:
        bits.append(f"+{drops} price drop{'' if drops == 1 else 's'}")
    if other:
        bits.append(f"+{other} other")
    return f"{title} ({', '.join(bits)})"


def as_push(changes: list[Change], *, limit: int = 12) -> str:
    """The body under push_title's heading, as plain text.

    ntfy's Android app does not render Markdown, and it shows the title above
    the body, so there is no markup and no headline here. New cars, then
    price drops, then the rest, each a short block with a short link; a
    single car gets no section heading.
    """
    ordered = in_order(changes)
    shown = ordered[:max(1, limit)]

    def block(change: Change, *, named: bool = True) -> list[str]:
        car = change.listing
        lines: list[str] = []
        if named:
            head = f"{car_name(car)} · {car.price_text}"
            if change.kind == Change.PRICE_DROP and _dropped(change):
                head += f", {_dropped(change)}"
            elif change.kind == Change.PRICED:
                head += " (was call for price)"
            note = _held_note(change)
            if note:
                head += f" ({note})"
            lines.append(head)
        facts = _push_facts(car)
        if change.kind == Change.PRICE_DROP and not named and _dropped(change):
            facts = f"{_dropped(change).capitalize()} · {facts}" if facts \
                else _dropped(change).capitalize()
        if facts:
            lines.append(facts)
        link = short_link(car)
        if link:
            lines.append(link)
        return lines

    if len(ordered) == 1:
        return "\n".join(block(ordered[0], named=False))

    sections = (
        ("NEW", [c for c in shown if c.kind in Change.NEW_TO_YOU]),
        ("PRICE DROPS", [c for c in shown if c.kind == Change.PRICE_DROP]),
        ("ALSO", [c for c in shown if c.kind not in Change.NEW_TO_YOU
                  and c.kind != Change.PRICE_DROP]),
    )
    out: list[str] = []
    for heading, members in sections:
        if not members:
            continue
        if out:
            out.append("")
        out.append(heading)
        for i, change in enumerate(members):
            if i:
                out.append("")
            out.extend(block(change))
    if len(ordered) > len(shown):
        out += ["", f"+{len(ordered) - len(shown)} more on the dashboard"]
    return "\n".join(out)


def as_sms(changes: list[Change]) -> str:
    """Deliberately terse - SMS is billed per segment."""
    lines = [headline(changes)]
    for change in changes[:5]:
        listing = change.listing
        lines.append(f"{_change_prefix(change)}{listing.display_title} "
                     f"{listing.price_text} {listing.mileage_text}")
        if listing.url:
            lines.append(listing.url)
    if len(changes) > 5:
        lines.append(f"+{len(changes) - 5} more")
    return "\n".join(lines)[:MAX_SMS]


def as_markdown(changes: list[Change], *, limit: int = 12) -> str:
    """Slack/Discord-flavoured markdown."""
    lines = [f"*{headline(changes)}*", ""]
    for change in changes[:limit]:
        listing = change.listing
        title = listing.display_title
        link = f"<{listing.url}|{title}>" if listing.url else title
        lines.append(f"{_change_prefix(change)}{link}")
        lines.append("_" + " | ".join(_facts(listing)) + "_")
    if len(changes) > limit:
        lines.append(f"_...and {len(changes) - limit} more._")
    return "\n".join(lines)


def as_telegram_html(changes: list[Change], *, limit: int = 12) -> str:
    """Telegram accepts a small HTML subset: b, i, a, code, pre, u, s."""
    lines = [f"<b>{_esc(headline(changes))}</b>"]
    for change in changes[:limit]:
        listing = change.listing
        title = _esc(listing.display_title)
        link = f'<a href="{_esc(listing.url)}">{title}</a>' if listing.url else f"<b>{title}</b>"
        parts = _parts(change)
        mark = _MARKS.get(change.kind, "")
        prefix = ""
        if parts:
            prefix = (f"{mark} " if mark else "") + \
                f"<b>{_esc(', '.join(parts))}</b> – "
        lines.append("")
        lines.append(f"{prefix}{link}")
        facts = _facts(listing)
        ratio = the_ratio(listing)
        if ratio:
            facts.append(ratio)
        lines.append(f"<i>{_esc(' | '.join(facts))}</i>")
        if change.kind in (Change.PRICE_DROP, Change.PRICE_RISE):
            moved = how_far_it_moved(change)
            aside = f" <i>({_esc(moved)})</i>" if moved else ""
            lines.append(f"<s>${change.old_price:,}</s> → "
                         f"<b>${change.new_price:,}</b>{aside}")
    if len(changes) > limit:
        lines.append(f"\n<i>...and {len(changes) - limit} more.</i>")
    return "\n".join(lines)


def as_email_html(changes: list[Change], *, limit: int = 25,
                  dashboard_url: str = "") -> str:
    """A self-contained HTML email.

    Inline styles only, table-free layout where possible, and every colour
    stated explicitly - email clients strip <style> blocks and some force a
    dark background, so nothing may depend on a stylesheet or a default.
    """
    cards: list[str] = []
    for change in changes[:limit]:
        listing = change.listing
        badge = _badge_html(change)

        photo = ""
        if listing.thumbnail:
            photo = (f'<a href="{_esc(listing.url)}"><img src="{_esc(listing.thumbnail)}" '
                     f'width="200" alt="" style="width:200px;max-width:38%;height:auto;'
                     f'border-radius:8px;display:block;border:0;"></a>')

        was = ""
        if change.kind in (Change.PRICE_DROP, Change.PRICE_RISE) and change.old_price:
            was = (f'<span style="color:#6b7280;text-decoration:line-through;'
                   f'font-size:14px;">${change.old_price:,}</span> ')
        moved = how_far_it_moved(change) if was else ""
        moved_html = (f'<span style="color:#6b7280;font-size:14px;font-weight:400;">'
                      f' ({_esc(moved)})</span>') if moved else ""

        cards.append(f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="margin:0 0 14px 0;background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;">
 <tr>
  <td style="padding:14px;" valign="top">{photo}</td>
  <td style="padding:14px 14px 14px 0;" valign="top">
   {badge}
   <div style="margin:8px 0 4px 0;font:600 17px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;">
    <a href="{_esc(listing.url)}" style="color:#111827;text-decoration:none;">{_esc(listing.display_title)}</a>
   </div>
   <div style="font:700 20px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#0f7b3f;margin:0 0 6px 0;">
    {was}{_esc(listing.price_text)}{moved_html}
   </div>
   <div style="font:400 14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#4b5563;">
    {' &#183; '.join(_esc(f) for f in (_facts(listing)[1:] + ([the_ratio(listing)] if the_ratio(listing) else [])) ) or 'No further details'}
   </div>
   <div style="margin-top:10px;">
    <a href="{_esc(listing.url)}" style="display:inline-block;background:#111827;color:#ffffff;
       font:600 13px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
       padding:9px 14px;border-radius:6px;text-decoration:none;">View on AutoTrader</a>
   </div>
  </td>
 </tr>
</table>""")

    more = (f'<p style="font:400 14px/1.5 -apple-system,Arial,sans-serif;color:#6b7280;">'
            f'&hellip;and {len(changes) - limit} more.</p>') if len(changes) > limit else ""
    dash = (f'<p style="margin-top:18px;"><a href="{_esc(dashboard_url)}" '
            f'style="color:#1d4ed8;font:400 14px/1.5 -apple-system,Arial,sans-serif;">'
            f'Open your dashboard</a></p>') if dashboard_url else ""

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:20px;background:#f3f4f6;">
<div style="max-width:640px;margin:0 auto;">
 <h1 style="font:700 20px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#111827;margin:0 0 16px 0;">
  {_esc(headline(changes))}
 </h1>
 {''.join(cards)}
 {more}
 {dash}
 <p style="font:400 12px/1.5 -apple-system,Arial,sans-serif;color:#9ca3af;margin-top:22px;">
  Sent by your AutoTrader watcher. Change what you get in config.json or the dashboard settings.
 </p>
</div></body></html>"""


def as_discord_embeds(changes: list[Change], *, limit: int = 10) -> list[dict[str, Any]]:
    """Discord caps a message at 10 embeds."""
    colours = {Change.NEW: 0x1D4ED8, Change.PRICE_DROP: 0x0F7B3F,
               Change.PRICE_RISE: 0x9A3412, Change.PRICED: 0x0F7B3F,
               Change.RELISTED: 0x1D4ED8, Change.QUALIFIED: 0x1D4ED8,
               Change.REMOVED: 0x6B7280}
    embeds: list[dict[str, Any]] = []
    for change in changes[:min(limit, 10)]:
        listing = change.listing
        fields = [{"name": "Price", "value": listing.price_text, "inline": True}]
        if listing.mileage_km is not None:
            fields.append({"name": "Odometer", "value": listing.mileage_text, "inline": True})
        if listing.location or listing.province:
            fields.append({"name": "Where", "value": listing.location or listing.province,
                           "inline": True})
        if change.kind in (Change.PRICE_DROP, Change.PRICE_RISE) and change.old_price:
            fields.append({"name": "Was", "value": f"${change.old_price:,}", "inline": True})
        embed: dict[str, Any] = {
            "title": listing.display_title[:250] or f"Listing {listing.id}",
            "url": listing.url or None,
            "color": colours.get(change.kind, 0x111827),
            "description": change.describe(),
            "fields": fields,
        }
        if listing.thumbnail:
            embed["thumbnail"] = {"url": listing.thumbnail}
        if listing.search_name:
            embed["footer"] = {"text": listing.search_name[:2000]}
        embeds.append(embed)
    return embeds


def as_json_payload(changes: list[Change], run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Machine-readable body for the custom-webhook channel."""
    return {
        "headline": headline(changes),
        "count": len(changes),
        "run": run or {},
        "changes": [{
            "kind": c.kind,
            "old_price": c.old_price,
            "new_price": c.new_price,
            "delta": c.delta,
            "listing": c.listing.to_dict(),
        } for c in changes],
    }
