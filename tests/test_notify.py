"""A broken notification channel must never cost a run."""
from datetime import datetime

from autotrader import clock
from autotrader import render
from autotrader.config import Config
from autotrader.listing import Listing
from autotrader import notifiers
from autotrader.notifiers import Notifier, Result, alert, dispatch, in_quiet_hours
from autotrader.state import Change


def sample():
    a = Listing(id="1", url="https://www.autotrader.ca/a/x/5_1_/", title="2022 Honda Civic",
                year=2022, make="Honda", model="Civic", trim="Type R", price=42000,
                mileage_km=30000, location="St. Catharines", drivetrain="FWD")
    b = Listing(id="2", url="https://www.autotrader.ca/a/y/19_2_/", title="2021 Honda Civic",
                year=2021, make="Honda", model="Civic", price=38000, mileage_km=52000)
    return [Change(Change.NEW, a), Change(Change.PRICE_DROP, b, old_price=43000, new_price=38000)]


class Boom(Notifier):
    name = "boom"
    def _send(self, changes, run): raise RuntimeError("channel is on fire")
    def _send_text(self, s, b): raise RuntimeError("still on fire")


class Fine(Notifier):
    name = "fine"
    def __init__(self): super().__init__({}, {}, {}); self.sent = []
    def _send(self, changes, run): self.sent.append(changes); return Result("fine", True)
    def _send_text(self, s, b): self.sent.append((s, b)); return Result("fine", True)


def test_one_failing_channel_does_not_stop_the_others():
    good = Fine()
    results = dispatch(Config.defaults(), sample(), notifiers=[Boom({}, {}, {}), good])
    assert [r.ok for r in results] == [False, True]
    assert good.sent


def test_a_failing_channel_never_raises():
    results = dispatch(Config.defaults(), sample(), notifiers=[Boom({}, {}, {})])
    assert results[0].ok is False and "on fire" in results[0].detail


def test_health_alerts_also_survive_a_broken_channel():
    results = alert(Config.defaults(), "subject", "body", notifiers=[Boom({}, {}, {}), Fine()])
    assert [r.ok for r in results] == [False, True]


def test_no_changes_means_no_message():
    good = Fine()
    assert dispatch(Config.defaults(), [], notifiers=[good]) == []
    assert not good.sent


def test_missing_configuration_is_reported_not_silent():
    results = dispatch(Config.defaults(), sample(), env={})
    assert results[0].skipped and "no notification channel" in results[0].detail


def test_channels_switch_on_by_themselves_once_their_secrets_exist():
    cfg = Config.defaults()
    assert cfg.active_channels({}) == []
    assert cfg.active_channels({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}) == ["telegram"]


def test_a_half_configured_channel_stays_off_and_says_what_is_missing():
    cfg = Config.defaults()
    status = cfg.channel_status({"TELEGRAM_BOT_TOKEN": "t"})["telegram"]
    assert not status["active"]
    assert status["missing"] == ["TELEGRAM_CHAT_ID"]


def test_twilio_stays_off_unless_explicitly_enabled():
    cfg = Config.defaults()
    env = {"TWILIO_SID": "s", "TWILIO_TOKEN": "t", "TWILIO_FROM": "+1", "TWILIO_TO": "+2"}
    assert "twilio" not in cfg.active_channels(env)   # it costs money, so opt-in


class TestQuietHours:
    S = {"quiet_hours": {"enabled": True, "start": "23:00", "end": "07:00"},
         "timezone": "America/Toronto"}

    def test_inside_the_window(self):
        assert in_quiet_hours(self.S, datetime(2026, 9, 9, 2, 0))
        assert in_quiet_hours(self.S, datetime(2026, 9, 9, 23, 30))

    def test_outside_the_window(self):
        assert not in_quiet_hours(self.S, datetime(2026, 9, 9, 12, 0))
        assert not in_quiet_hours(self.S, datetime(2026, 9, 9, 7, 0))

    def test_disabled_means_never_quiet(self):
        assert not in_quiet_hours({"quiet_hours": {"enabled": False}}, datetime(2026, 9, 9, 2, 0))

    def test_a_broken_time_does_not_silence_everything(self):
        bad = {"quiet_hours": {"enabled": True, "start": "nonsense", "end": "07:00"}}
        assert not in_quiet_hours(bad, datetime(2026, 9, 9, 2, 0))


class TestRendering:
    def test_the_headline_leads_with_the_drop_and_its_figure(self):
        """A title is a lock screen, not an index.

        "1 new listing, 1 price drop" costs a look at the phone to find out
        what dropped and by how much, which is the entire content.
        """
        line = render.headline(sample())
        assert line.startswith("AutoTrader: ")
        assert "$" in line, line
        assert "1 more change" in line, line

    def test_one_change_names_the_car_and_the_figure(self):
        [only] = [c for c in sample() if c.kind == Change.PRICE_DROP]
        line = render.headline([only])
        assert "down $" in line and only.listing.price_text in line, line

    def test_no_changes_says_so(self):
        assert render.headline([]) == "AutoTrader: no changes"

    def test_without_a_drop_it_counts_the_kinds(self):
        news = [c for c in sample() if c.kind == Change.NEW]
        assert render.headline(news * 2) == "AutoTrader: 2 new listings"

    def test_sms_stays_within_one_message_budget(self):
        assert len(render.as_sms(sample())) <= render.MAX_SMS

    def test_telegram_html_escapes_user_text(self):
        bad = Listing(id="1", url="http://x", title='<script>alert("x")</script>')
        out = render.as_telegram_html([Change(Change.NEW, bad)])
        assert "<script>" not in out and "&lt;script&gt;" in out

    def test_email_html_escapes_user_text(self):
        bad = Listing(id="1", url="http://x", title='"><img src=x onerror=alert(1)>')
        out = render.as_email_html([Change(Change.NEW, bad)])
        # The payload may appear as inert text, but never as a live tag or as
        # an attribute that escapes the href it sits in.
        assert "<img src=x" not in out
        assert '"><img' not in out
        assert "&lt;img src=x onerror=alert(1)&gt;" in out

    def test_discord_respects_its_ten_embed_limit(self):
        many = [Change(Change.NEW, Listing(id=str(i), url="http://x", title=f"Car {i}"))
                for i in range(30)]
        assert len(render.as_discord_embeds(many, limit=25)) == 10

    def test_a_price_drop_shows_both_prices(self):
        out = render.as_text(sample())
        assert "$5,000 off" in out and "$38,000" in out

    def test_the_trim_is_not_repeated_after_the_title(self):
        """In the body. The headline names the car too, and should."""
        body = render.as_text([sample()[0]]).split("\n", 1)[1]
        assert body.count("Type R") == 1, body

    def test_a_car_with_no_price_still_renders(self):
        listing = Listing(id="1", url="http://x", title="2020 Honda Civic")
        out = render.as_text([Change(Change.NEW, listing)])
        assert "Price not listed" in out

    def test_the_webhook_payload_is_json_serialisable(self):
        import json
        json.dumps(render.as_json_payload(sample(), {"ok": True}))


class TestWhatThePushCarries:
    """The alert is half the product, so what rides on it is tested.

    All of this is checked against the request the notifier builds, never by
    sending: a suite that touches ntfy.sh is a suite that fails when ntfy.sh
    is having a bad day, and it would deliver to the topic on a real phone.
    """

    def _posted(self, changes, settings=None, monkeypatch=None, config=None):
        sent = {}

        class FakeResponse:
            ok = True
            status_code = 200
            text = ""

        def fake_post(url, data=None, headers=None, timeout=None):
            sent["url"] = url
            sent["headers"] = headers or {}
            sent["body"] = (data or b"").decode("utf-8")
            return FakeResponse()

        monkeypatch.setattr(notifiers.requests, "post", fake_post)
        # `settings or {...}` would swallow an explicit empty dict, which is
        # exactly the case one of these tests is about.
        if settings is None:
            settings = {"dashboard_url": "https://example.test/watch"}
        n = notifiers.NtfyNotifier(config or {"topic": "t"}, {}, settings)
        result = n.send(changes, {})
        assert result.ok, result.detail
        # Read the title the way ntfy does. It goes out RFC 2047-encoded when
        # it carries anything outside ASCII - which the "·" in every heading
        # does - so asserting on the raw header would be asserting on base64.
        from email.header import decode_header
        raw = sent["headers"].get("Title", "")
        sent["title"] = "".join(
            part.decode(enc or "utf-8") if isinstance(part, bytes) else part
            for part, enc in decode_header(raw))
        return sent

    def test_a_new_car_outranks_a_price_drop(self, monkeypatch):
        """New listings rank much higher than price drops.

        This used to wake the phone hardest for a drop and treat a new car as
        routine, which is backwards: a car you have never seen is the top of
        the scale, a drop is an ordinary notification, and a car leaving the
        market is barely one."""
        conf = {"topic": "t", "priority": "default", "priority_new": "max"}
        new = [c for c in sample() if c.kind == Change.NEW]
        drop = [c for c in sample() if c.kind == Change.PRICE_DROP]
        gone = [Change(Change.REMOVED, sample()[0].listing)]

        assert self._posted(new, monkeypatch=monkeypatch,
                            config=conf)["headers"]["Priority"] == "max"
        # "default" is ntfy's own default, so it is not sent at all.
        assert "Priority" not in self._posted(drop, monkeypatch=monkeypatch,
                                              config=conf)["headers"]
        assert self._posted(gone, monkeypatch=monkeypatch,
                            config=conf)["headers"]["Priority"] == "low"

    def test_a_drop_riding_with_a_new_car_gets_the_new_cars_priority(self, monkeypatch):
        """It is one notification, and it is about the new car."""
        conf = {"topic": "t", "priority_new": "max"}
        new = [c for c in sample() if c.kind == Change.NEW][:1]
        drop = [c for c in sample() if c.kind == Change.PRICE_DROP][:1]
        drop[0].rider = True
        sent = self._posted(drop + new, monkeypatch=monkeypatch, config=conf)
        assert sent["headers"]["Priority"] == "max"
        assert sent["title"].startswith("New "), sent["title"]

    def test_it_opens_the_car_on_the_dashboard_not_the_search_page(self, monkeypatch):
        drop = [c for c in sample() if c.kind == Change.PRICE_DROP]
        click = self._posted(drop, monkeypatch=monkeypatch)["headers"]["Click"]
        assert click == f"https://example.test/watch/#/listing/{drop[0].listing.id}"

    def test_with_no_dashboard_it_falls_back_to_the_listing(self, monkeypatch):
        drop = [c for c in sample() if c.kind == Change.PRICE_DROP]
        click = self._posted(drop, settings={}, monkeypatch=monkeypatch)["headers"]["Click"]
        assert click == drop[0].listing.url

    def test_one_change_carries_the_photo(self, monkeypatch):
        listing = Listing(id="9", url="http://x", title="2022 Honda Civic", price=90000,
                          images=["https://pics.test/a.jpg"])
        sent = self._posted([Change(Change.NEW, listing)], monkeypatch=monkeypatch)
        assert sent["headers"]["Attach"] == "https://pics.test/a.jpg"

    def test_a_digest_led_by_a_new_car_carries_that_cars_photo(self, monkeypatch):
        """A picture is only wrong when nothing says which car it is.

        That was the reason a digest used to carry none: forty cars and one
        photo. The heading now names one car - the new one, first - so its
        photo is the one that belongs there, and it is most of what makes
        someone open the ad."""
        car = Listing(id="9", url="http://x", title="2022 Honda Civic", price=90000,
                      images=["https://pics.test/a.jpg"])
        other = Listing(id="8", url="http://y", title="2019 Honda Civic", price=70000,
                        images=["https://pics.test/b.jpg"])
        many = [Change(Change.PRICE_DROP, other, old_price=74000, new_price=70000),
                Change(Change.NEW, car)]
        assert self._posted(many, monkeypatch=monkeypatch)["headers"]["Attach"] \
            == "https://pics.test/a.jpg"

    def test_a_digest_of_drops_carries_no_photo(self, monkeypatch):
        """No new car to lead with, so no single car the picture is of."""
        a = Listing(id="9", url="http://x", title="2022 Honda Civic", price=90000,
                    images=["https://pics.test/a.jpg"])
        b = Listing(id="8", url="http://y", title="2019 Honda Civic", price=70000,
                    images=["https://pics.test/b.jpg"])
        drops = [Change(Change.PRICE_DROP, a, old_price=95000, new_price=90000),
                 Change(Change.PRICE_DROP, b, old_price=74000, new_price=70000)]
        assert "Attach" not in self._posted(drops, monkeypatch=monkeypatch)["headers"]

    def test_the_title_carries_the_figure(self, monkeypatch):
        drop = [c for c in sample() if c.kind == Change.PRICE_DROP]
        title = self._posted(drop, monkeypatch=monkeypatch)["title"]
        assert "down $" in title and "change" not in title, title

    def test_the_title_survives_the_trip_in_utf8(self, monkeypatch):
        """It used to be folded to ASCII, which turned every "·" into "?"."""
        new = [c for c in sample() if c.kind == Change.NEW][:1]
        sent = self._posted(new, monkeypatch=monkeypatch)
        assert sent["headers"]["Title"].isascii()
        assert "\u00b7" in sent["title"], sent["title"]

    def test_there_is_no_markdown_header(self, monkeypatch):
        """ntfy renders Markdown in the web app only, and there it joins
        lines not separated by a blank one - the same body read as a run-on
        paragraph in a browser and as separate lines on the phone."""
        new = [c for c in sample() if c.kind == Change.NEW][:1]
        assert "Markdown" not in self._posted(new, monkeypatch=monkeypatch)["headers"]


class TestWhatTheAlertActuallySays:
    """The part of this product nobody screenshots.

    A sweep for user-visible strings derived rather than stored found four
    defects here that had never been looked at, because looking means reading
    a push notification on a phone rather than a page in a browser.
    """

    def car(self, **kw):
        from autotrader.listing import Listing
        base = dict(id="00000001", title="Honda Civic LX", year=2019,
                    make="Honda", model="Civic", price=21000, price_source="detail",
                    mileage_km=103000, location="Oakville", province="ON")
        base.update(kw)
        return Listing(**base)

    def test_a_dealers_feature_list_is_not_repeated_under_the_name(self):
        """The page has trimmed this since the day it was written; every
        notification repeated it verbatim."""
        from autotrader import render
        facts = render._facts(self.car(
            trim="LX * NO ACCIDENTS * ONE OWNER * CERTIFIED"))
        assert not any("ACCIDENTS" in f for f in facts), facts

    def test_a_field_a_dealer_left_as_na_is_not_a_specification(self):
        from autotrader import render
        facts = render._facts(self.car(transmission="n/a"))
        assert "n/a" not in facts, facts

    def test_an_accented_name_survives_the_ntfy_header(self):
        """HTTP headers are latin-1 at best. This encoded UTF-8 and decoded
        the bytes as latin-1, which is the definition of mojibake - and
        dealers on a bilingual site do type "ÉDITION"."""
        from autotrader.notifiers import _ascii_header
        out = _ascii_header("AutoTrader: 2025 HONDA CIVIC TYPE R ÉDITION $41,000")
        assert out == "AutoTrader: 2025 HONDA CIVIC TYPE R EDITION $41,000"
        assert out.isascii()

    def test_a_header_of_pure_non_latin_still_says_something(self):
        from autotrader.notifiers import _ascii_header
        assert _ascii_header("中文").isascii()


class TestTheCoverageAlertCountsSlots:
    """The "79.2% - 51 of 48 expected checks" bug, in the alert.

    The dashboard printed a percentage derived from slots beside a fraction
    derived from runs. That was fixed on the page and left standing here,
    where the same sentence is sent by email - which is how a bug survives a
    fix: the second copy is in the channel nobody looks at.
    """

    def test_the_fraction_matches_the_percentage(self, tmp_path, monkeypatch):
        from datetime import timedelta
        from autotrader import events
        from autotrader.config import Config
        from autotrader.state import State

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("health.expected_interval_minutes", 120)
        cfg.save()
        state = State(path=tmp_path / "state.json")
        now = clock.now()
        # Four checks in a 24-hour window that asks for twelve.
        state.data["runs"] = [
            {"at": (now - timedelta(hours=h)).isoformat(timespec="seconds"),
             "ok": True, "searches_run": 1, "listings_seen": 5}
            for h in (1, 7, 13, 19)]
        note = events.thin_coverage(cfg, state, {}, now=now)
        assert note, "a quarter of the slots covered should be worth saying"
        body = note["body"]
        assert "half-hours" not in body, body
        assert "2-hour slots" in body, body
        # The number in the sentence is the number the percentage came from.
        import re
        got = re.search(r"(\d+) of (\d+) 2-hour slots", body)
        assert got, body
        covered, expected = int(got.group(1)), int(got.group(2))
        assert round(covered / expected * 100, 1) == note["pct"], (body, note["pct"])


class TestTheEmailIsHtmlNotEscapedHtml:
    def test_the_fact_separator_is_a_separator_not_its_own_source_code(self):
        """`_esc(' &#183; '.join(...))` escaped the ampersand first, so the
        later .replace could never match and the email showed the characters
        "&#183;" between every fact."""
        from autotrader.listing import Listing
        from autotrader.state import Change
        from autotrader import render
        car = Listing(id="x", title="Honda Civic", year=2018, make="Honda", model="Civic",
                      price=24000, price_source="detail", mileage_km=50000,
                      color="Black", location="Oakville")
        html = render.as_email_html([Change(kind=Change.NEW, listing=car)])
        assert "&amp;#183;" not in html
        assert "&#183;" in html, "the separator went missing entirely"

    def test_a_dealer_title_with_an_ampersand_is_still_escaped(self):
        """The fix must not stop escaping the thing escaping is for."""
        from autotrader.listing import Listing
        from autotrader.state import Change
        from autotrader import render
        car = Listing(id="x", title="Honda Civic", year=2018, make="Honda", model="Civic",
                      price=1, price_source="detail", color="Black & Gold")
        html = render.as_email_html([Change(kind=Change.NEW, listing=car)])
        assert "Black &amp; Gold" in html


class TestTheOverflowPastTheDigestCapStaysOwed:
    """A digest names at most `max_listings_per_message` cars and ends
    "...and 8 more." The eight it did not name have not been told to you."""

    def test_only_the_cars_the_message_named_are_marked_told(self, bench):
        """The bug this pins down: all twenty were marked notified, so the
        eight that were never named were never mentioned again by any run,
        while the dashboard said "You were told about this" beside each."""
        from autotrader.listing import Listing
        from autotrader.state import Change, State

        bench.cfg.set("notifications.max_listings_per_message", 12)
        bench.cfg.save()
        bench.run()                      # baseline the search

        state = State.load(bench.path / "state.json")
        changes = []
        for i in range(20):
            listing = Listing(id=f"ov{i}",
                              url=f"https://www.autotrader.ca/a/ov{i}",
                              title=f"Car {i}", price=40000 + i, year=2018,
                              make="Honda", model="Civic")
            # Recorded first: mark_notified stamps a stored row, and a change
            # about a car with no row would prove nothing either way.
            state.record(listing)
            changes.append(Change(Change.NEW, listing))
        state.defer(changes)
        state.save()

        bench.run(minutes_later=200)

        after = State.load(bench.path / "state.json")
        stamped = {c.listing.id for c in changes
                   if (after.listings.get(c.listing.id) or {}).get("notified_at")}
        assert len(stamped) <= 12, (
            f"{len(stamped)} cars were marked as told about by a message that "
            f"can only name 12")
        unnamed = {c.listing.id for c in changes} - stamped
        assert unnamed, "the cap did not bite, so this test proved nothing"

        still_owed = {c.listing.id for c in after.pending_changes()}
        assert unnamed <= still_owed, (
            "a car the digest could not name is still owed to you and has to "
            "come back on the next run; missing: "
            f"{sorted(unnamed - still_owed)}")

    def test_and_no_car_is_ever_named_twice(self, bench):
        """The other half of the same contract, and the more important half.

        Finishing the job on the next run is only worth having if it cannot
        turn into the bot telling you about the same car again and again -
        which is how a watcher stops being read.

        Measured on `notified_at`, not on what reached the sink. `dispatch`
        is handed every change and each channel applies the cap while
        rendering, so the sink sees all seventeen on the first run and the
        twelve still owed on the second - which looks like a duplicate and is
        not one. `notified_at` is the bot's own record of which cars a
        message actually named, and that is the thing that must never be
        written twice.
        """
        from autotrader.listing import Listing
        from autotrader.state import Change, State

        cap = 5
        bench.cfg.set("notifications.max_listings_per_message", cap)
        bench.cfg.save()
        bench.run()

        state = State.load(bench.path / "state.json")
        wanted = []
        for i in range(17):
            listing = Listing(id=f"tw{i}",
                              url=f"https://www.autotrader.ca/a/tw{i}",
                              title=f"Car {i}", price=40000 + i, year=2018,
                              make="Honda", model="Civic")
            state.record(listing)
            wanted.append(Change(Change.NEW, listing))
        state.defer(wanted)
        state.save()

        ids = {c.listing.id for c in wanted}
        stamps: dict[str, str] = {}
        for run_no in range(6):
            bench.run(minutes_later=200 * (run_no + 1))
            after = State.load(bench.path / "state.json")
            now = {i: (after.listings.get(i) or {}).get("notified_at")
                   for i in ids}
            now = {i: at for i, at in now.items() if at}
            for i, at in stamps.items():
                assert now.get(i) == at, (
                    f"{i} was named again on run {run_no}: it was told to you "
                    f"at {at} and has just been told to you at {now.get(i)}")
            fresh = set(now) - set(stamps)
            assert len(fresh) <= cap, (
                f"run {run_no} claims to have named {len(fresh)} cars in a "
                f"message that can only name {cap}")
            stamps = now

        assert set(stamps) == ids, (
            "every car has to be named eventually; never named: "
            f"{sorted(ids - set(stamps))}")
