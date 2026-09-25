"""No channel may quietly not know about a kind of change.

Three renderers kept their own list of what to call each kind of change.
Telegram's knew four of seven; the email's knew five. A relisted car and a
car that came back inside your price ceiling therefore arrived on those
channels with no label at all - rendered exactly like a listing with nothing
to report, which is the one thing they are definitely not.

This is the class, not the instance: the test is over the kinds the bot can
produce, so adding an eighth one fails here until every channel can say it.
"""
from __future__ import annotations

import pytest

from autotrader import clock, render
from autotrader.listing import Listing
from autotrader.state import Change

#: Every kind Change can carry, read off the class rather than typed out, so
#: a new one cannot be added without this file noticing.
KINDS = sorted(v for k, v in vars(Change).items()
               if k.isupper() and isinstance(v, str))

CAR = Listing(id="1", url="https://example.invalid/car", title="2019 Honda Civic",
              price=32000, price_source="detail", search_id="s")


def a_change(kind: str, **kw) -> Change:
    return Change(kind, CAR, old_price=36000, new_price=32000, **kw)


def test_the_list_of_kinds_is_the_one_the_bot_actually_has():
    assert set(KINDS) == {"new", "price_drop", "price_rise", "priced",
                          "removed", "relisted", "qualified"}


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_words(kind):
    words = render._change_words(a_change(kind))
    # NEW is the one kind the headline already names; everything else must
    # say what it is, because "a car" and "a car that came back" look the
    # same otherwise.
    assert words or kind == Change.NEW, f"{kind} renders as an unlabelled car"


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_a_badge_and_a_colour(kind):
    change = a_change(kind)
    assert render._badge_words(change).strip(), kind
    assert kind in render._BADGE_COLOURS, f"{kind} would fall back to grey"


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_a_mark_where_a_mark_is_shown(kind):
    assert kind in render._MARKS or kind == Change.NEW, kind


def test_no_two_kinds_read_the_same():
    """Two kinds wearing one label is the bug with extra steps."""
    words = {kind: render._change_words(a_change(kind)) for kind in KINDS}
    seen: dict[str, str] = {}
    for kind, text in words.items():
        if not text:
            continue
        assert text not in seen, f"{kind} and {seen[text]} both say {text!r}"
        seen[text] = kind


@pytest.mark.parametrize("renderer", [
    render.as_text, render.as_sms, render.as_markdown,
    render.as_telegram_html, render.as_email_html,
])
def test_every_channel_distinguishes_every_kind(renderer):
    """One car, seven ways, rendered one at a time: seven different outputs."""
    rendered = {}
    for kind in KINDS:
        out = renderer([a_change(kind)])
        assert isinstance(out, str) and out.strip(), kind
        rendered[kind] = out
    # A removal and a relisting must not produce byte-identical messages.
    for a in KINDS:
        for b in KINDS:
            if a < b:
                assert rendered[a] != rendered[b], \
                    f"{renderer.__name__} renders {a} and {b} identically"


class TestAnAlertThatWaited:
    """Quiet hours and a dead channel both delay delivery by hours.

    The bot works the change out on one run and sends it on a later one. Sent
    without a word, a nine-hour-old car reads as news, and "get in early" is
    the advice it least deserves.
    """

    def test_a_held_alert_says_how_long_it_waited(self):
        clock.freeze("2026-09-14T20:00:00Z")
        change = a_change(Change.NEW, held_since="2026-09-14T11:00:00Z")
        assert "held 9 hours" in render._change_prefix(change)
        assert "held 9 hours" in render.as_text([change])
        assert "held 9 hours" in render.as_telegram_html([change])
        assert "held 9 hours" in render.as_email_html([change])

    def test_an_alert_sent_on_the_run_that_found_it_says_nothing(self):
        clock.freeze("2026-09-14T20:00:00Z")
        assert render._held_note(a_change(Change.NEW)) == ""
        assert render._held_note(
            a_change(Change.NEW, held_since="2026-09-14T19:58:00Z")) == "", \
            "two minutes is not worth a word"

    def test_a_nonsense_stamp_does_not_stop_the_alert(self):
        """Losing every alert in a batch to a bad timestamp is not a trade."""
        assert render._held_note(a_change(Change.NEW, held_since="soon")) == ""
        assert render.as_text([a_change(Change.NEW, held_since="soon")])
