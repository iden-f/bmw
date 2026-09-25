"""What actually arrives on a phone.

Written after a real complaint: the alerts wore the generic ntfy bell, they
said less about a price drop than the dashboard did, and getting the topic
onto a handset meant typing twenty-nine random characters with a thumb.
"""

from __future__ import annotations

import json

import pytest

from autotrader import dashboard, notifiers, render
from autotrader.listing import Listing
from autotrader.notifiers import NtfyNotifier
from autotrader.state import Change

HOME = "https://example.github.io/watch"


def ntfy(settings=None, config=None):
    n = NtfyNotifier.__new__(NtfyNotifier)
    n.config = {"server": "https://ntfy.sh", "topic": "t", **(config or {})}
    n.settings = {"dashboard_url": HOME, **(settings or {})}
    n.env = {}
    return n


def drop():
    car = Listing(id="abc", title="2020 Honda Civic Type R", price=30000,
                  mileage_km=120000, location="Ottawa",
                  url="https://www.autotrader.ca/a/honda/civic-type-r/1")
    return Change(Change.PRICE_DROP, car, old_price=30500, new_price=30000)


class TestTheIcon:
    def test_it_points_at_the_page_the_bot_publishes(self):
        """The dashboard's own icon, already served next to the page. No
        third party, no upload, nothing to keep in sync by hand."""
        assert ntfy()._icon() == f"{HOME}/icon-192.png"

    def test_a_fork_gets_its_own_without_configuring_anything(self):
        assert ntfy({"dashboard_url": "https://someone.github.io/theirs"})._icon() \
            == "https://someone.github.io/theirs/icon-192.png"

    def test_no_dashboard_means_no_icon_rather_than_a_broken_one(self):
        """ntfy fetches the URL on the handset. A relative or empty one is a
        request that fails silently, which looks exactly like the bug this
        header was added to fix."""
        assert ntfy({"dashboard_url": ""})._icon() == ""
        assert ntfy({"dashboard_url": "not a url"})._icon() == ""

    def test_it_can_be_overridden(self):
        assert ntfy(config={"icon": "https://example.com/a.png"})._icon() \
            == "https://example.com/a.png"

    def test_the_file_it_names_is_one_the_repository_actually_publishes(self):
        from pathlib import Path
        assert Path("docs/icon-192.png").exists()


class TestTheButtons:
    def test_one_tap_to_the_advertisement(self):
        actions = json.loads(ntfy()._actions(drop(), 1, HOME))
        assert len(actions) == 1
        # "Open ad", not "Open on AutoTrader": a lock-screen button has room
        # for about twelve characters before Android starts cutting it.
        assert actions[0]["label"] == "Open ad"
        assert actions[0]["url"] == "https://www.autotrader.ca/a/honda/civic-type-r/1"

    def test_a_digest_also_offers_the_rest_of_it(self):
        actions = json.loads(ntfy()._actions(drop(), 5, HOME))
        assert [a["label"] for a in actions] == [
            "Open ad", "All 5 on the dashboard"]

    def test_a_comma_in_a_listing_url_does_not_split_the_button_in_two(self):
        """ntfy's shorthand for actions is comma-separated, and a URL with a
        comma in it silently becomes two broken buttons. JSON cannot."""
        change = drop()
        change.listing.url = "https://www.autotrader.ca/a/honda/civic,type-r/1"
        actions = json.loads(ntfy()._actions(change, 1, HOME))
        assert actions[0]["url"].endswith("civic,type-r/1")

    def test_a_car_with_no_link_gets_no_button(self):
        change = drop()
        change.listing.url = ""
        assert ntfy()._actions(change, 1, "") == ""

    def test_ntfy_takes_three_at_most(self):
        assert len(json.loads(ntfy()._actions(drop(), 9, HOME))) <= 3


class TestItFitsInOneNotification:
    def test_a_short_message_is_untouched(self):
        assert notifiers._fits_ntfy("two\nlines") == b"two\nlines"

    def test_a_long_one_is_cut_rather_than_turned_into_a_download(self):
        """Past 4,096 bytes ntfy does not refuse - it converts the push into a
        file attachment, which arrives as a download instead of as something
        readable on a lock screen."""
        body = "\n".join(f"2020 Honda Civic line {i}" for i in range(400))
        out = notifiers._fits_ntfy(body)
        assert len(out) <= notifiers.NTFY_MAX_BODY
        assert out.decode().endswith("The dashboard has all of it.")

    def test_it_counts_bytes_and_not_characters(self):
        body = "é" * 4000                      # two bytes each
        assert len(notifiers._fits_ntfy(body)) <= notifiers.NTFY_MAX_BODY

    def test_it_cuts_at_a_line_so_the_last_car_is_a_whole_car(self):
        body = "\n".join(f"{'x' * 200} car {i}" for i in range(60))
        kept = notifiers._fits_ntfy(body).decode().split("\n\n")[0]
        assert kept.rstrip().endswith(tuple(str(n) for n in range(60)))


class TestEveryHeaderSurvivesTheWire:
    def test_an_accented_car_does_not_lose_the_alert(self, monkeypatch):
        """requests encodes headers as latin-1. A non-ASCII character in one
        raises UnicodeEncodeError, which the channel files as a failure - so
        the alert never arrives and only the log knows."""
        sent = {}

        class Reply:
            ok, status_code, text = True, 200, ""

        def post(url, data=None, headers=None, timeout=None):
            sent.update(headers)
            for key, value in headers.items():
                value.encode("latin-1")       # what requests will do
            return Reply()

        monkeypatch.setattr(notifiers.requests, "post", post)
        change = drop()
        change.listing.title = "2020 HONDA CIVIC TYPE R ÉDITION"
        change.listing.url = "https://www.autotrader.ca/a/café/1"
        ntfy()._post("Prix baissé de 500 $", "corps", click=change.listing.url,
                     actions=ntfy()._actions(change, 1, HOME))
        assert all(v.isascii() for v in sent.values())


class TestARefusalIsNotRetriedForever:
    def test_a_rejected_request_is_a_permanent_failure(self, monkeypatch):
        """A header ntfy will not take is a header it will not take on the
        thousandth attempt. Without this the channel failed every half hour
        and never retired itself, so nothing ever said so."""
        class Reply:
            ok, status_code, text = False, 400, "invalid Actions header"
        monkeypatch.setattr(notifiers.requests, "post",
                            lambda *a, **k: Reply())
        with pytest.raises(RuntimeError) as caught:
            ntfy()._post("t", "b")
        assert notifiers.is_permanent_failure(str(caught.value))

    def test_rate_limiting_is_not(self):
        """ntfy.sh allows 250 messages a day. Hitting that is a reason to
        wait, not a reason to switch the channel off."""
        assert not notifiers.is_permanent_failure("HTTP 429: ntfy failed - slow down")

    def test_the_server_falling_over_is_not(self):
        assert not notifiers.is_permanent_failure("HTTP 503: ntfy failed - try later")


class TestItSaysAsMuchAsThePage:
    def test_a_drop_carries_both_prices_the_percentage_and_the_ratio(self):
        text = render.as_text([drop()])
        assert "$30,500 to $30,000, down $500 (1.6%)" in text
        assert "$250 per 1,000 km" in text

    def test_a_new_listing_carries_the_ratio(self):
        car = Listing(id="a", title="2020 Honda Civic", price=30000, mileage_km=100000)
        assert "$300 per 1,000 km" in render.as_text([Change(Change.NEW, car)])

    def test_a_car_with_no_odometer_says_nothing_rather_than_a_wrong_number(self):
        car = Listing(id="a", title="2020 Honda Civic", price=30000)
        assert "per 1,000 km" not in render.as_text([Change(Change.NEW, car)])

    def test_the_move_is_worded_once_for_every_channel(self):
        assert render.the_move(drop()) == "$30,500 to $30,000, down $500 (1.6%)"
        assert render.how_far_it_moved(drop()) == "down $500, 1.6%"

    def test_a_change_with_no_figures_produces_no_claim(self):
        car = Listing(id="a", title="2020 Honda Civic")
        assert render.the_move(Change(Change.REMOVED, car)) == ""
        assert render.how_far_it_moved(Change(Change.REMOVED, car)) == ""

    def test_telegram_and_email_carry_the_same_facts(self):
        for html in (render.as_telegram_html([drop()]), render.as_email_html([drop()])):
            assert "per 1,000 km" in html
            assert "1.6%" in html


class TestGettingItOntoAPhone:
    def test_the_payload_carries_a_scannable_code_for_the_topic(self):
        from autotrader import qr
        svg = dashboard._subscribe_qr("https://ntfy.sh", "autotrader-abc123")
        assert svg.startswith("<svg")
        # The same code the encoder produces for that address, module for
        # module - so the picture on the page is the address on the page.
        assert svg == qr.encode("https://ntfy.sh/autotrader-abc123",
                                level="M").to_svg(
            dark="currentColor", label="Subscribe to autotrader-abc123 on ntfy")

    def test_no_topic_means_no_code(self):
        assert dashboard._subscribe_qr("https://ntfy.sh", "") == ""

    def test_a_code_that_cannot_be_drawn_does_not_stop_the_dashboard(self):
        """A QR is decoration on a page whose job is numbers. Failing to draw
        one must never be the reason a check has nothing to publish."""
        assert dashboard._subscribe_qr("https://ntfy.sh", "x" * 5000) == ""

    def test_the_page_offers_the_three_steps_and_the_topic_to_copy(self):
        from pathlib import Path
        source = Path("docs/app.js").read_text()
        assert "function onYourPhone" in source
        assert "data-copy=" in source
        for store in ("apps.apple.com", "play.google.com", "f-droid.org"):
            assert store in source
