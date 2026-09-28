"""End-to-end behaviour, including the failure modes that killed version 1."""
import json


from autotrader import notifiers, runner as runner_mod
from autotrader.config import Config
from autotrader.http import BlockedError, FetchError
from autotrader.notifiers import Notifier
from autotrader.runner import run
from autotrader.state import Change, State

from .helpers import FakeFetcher, next_check

SEARCH = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&prx=-2&loc=K1P"


def test_a_first_run_finds_and_announces_every_car(bench):
    report = bench.run()
    assert report.ok and report.searches_run == 1
    assert report.listings_seen == 3 and report.new == 3
    assert len(bench.sink.digests) == 1
    assert len(bench.sink.digests[0]) == 3


def test_everything_arrives_as_one_digest_not_one_message_per_car(bench):
    """An email and an SMS per listing is how you get your Gmail account
    rate-limited and your Twilio bill run up."""
    bench.run()
    assert len(bench.sink.digests) == 1


def test_running_again_on_an_unchanged_page_says_nothing(bench):
    bench.run()
    bench.sink.digests.clear()
    report = bench.run()
    assert report.new == 0 and report.price_drops == 0
    assert bench.sink.digests == []


def test_a_card_price_that_disagrees_with_the_listing_page_is_not_a_price_drop(bench):
    """The detail page is authoritative; a card that quotes a different number
    must not raise a false alarm."""
    bench.run()
    bench.sink.digests.clear()
    report = bench.run(search_html=bench.cards.replace("$102,199", "$94,000"))
    assert report.price_drops == 0
    assert bench.sink.digests == []


def test_a_real_price_drop_is_announced_once(bench, archive_html):
    bench.run()
    bench.sink.digests.clear()
    cheaper = {i: archive_html(i) for i in ("13166607", "13221555")}
    cheaper["68819631"] = (archive_html("68819631")
                           .replace('"price":"102199"', '"price":"94000"')
                           .replace('"price": "102199"', '"price": "94000"'))

    def go(cards):
        return run(bench.cfg, State.load(bench.path / "state.json"),
                   fetcher=FakeFetcher(cards, cheaper))

    report = go(bench.cards.replace("$102,199", "$94,000"))
    assert report.price_drops == 1
    change = bench.sink.digests[0][0]
    assert change.kind == Change.PRICE_DROP
    assert change.old_price == 102199 and change.new_price == 94000

    bench.sink.digests.clear()
    assert go(bench.cards.replace("$102,199", "$94,000")).price_drops == 0
    assert bench.sink.digests == []


def test_a_blocked_site_does_not_destroy_what_we_already_knew(bench):
    """The bug that broke the first bot: an exception before a single save at
    the end of the run threw away every id, so the next run announced them all."""
    bench.run()
    before = json.loads((bench.path / "state.json").read_text())["listings"]

    report = bench.run(fail=BlockedError("anti-bot page"))
    assert not report.ok and report.searches_failed == 1

    after = json.loads((bench.path / "state.json").read_text())["listings"]
    assert set(after) == set(before)

    # And the recovery run stays quiet rather than re-announcing everything.
    bench.sink.digests.clear()
    assert bench.run().new == 0
    assert bench.sink.digests == []


def test_a_network_failure_is_reported_not_raised(bench):
    report = bench.run(fail=FetchError("connection reset"))
    assert not report.ok
    assert "connection reset" in " ".join(report.errors)


def test_repeated_failures_raise_a_health_alert(bench):
    for _ in range(3):
        bench.run(fail=BlockedError("anti-bot page"))
    assert bench.sink.alerts, "the bot must say when it has stopped working"
    subject, body = bench.sink.alerts[-1]
    assert "needs attention" in subject.lower()
    assert "Example search" in body


def test_the_health_alert_is_sent_once_per_outage_not_every_check(bench):
    """Twelve failed checks were eight high-priority pushes in four hours."""
    bench.run()
    for _ in range(12):
        bench.run(fail=BlockedError("anti-bot page"))
    assert len(bench.sink.alerts_matching("needs attention")) == 1


def test_a_long_outage_is_mentioned_again_each_day(bench):
    bench.run()
    for _ in range(3):
        bench.run(fail=BlockedError("anti-bot page"))
    assert len(bench.sink.alerts_matching("needs attention")) == 1
    next_check(minutes=24 * 60)
    bench.run(fail=BlockedError("anti-bot page"))
    assert len(bench.sink.alerts_matching("needs attention")) == 2


def test_a_new_outage_after_a_recovery_is_told(bench):
    bench.run()
    for _ in range(3):
        bench.run(fail=BlockedError("anti-bot page"))
    bench.run()
    for _ in range(3):
        bench.run(fail=BlockedError("anti-bot page"))
    assert len(bench.sink.alerts_matching("needs attention")) == 2


def test_the_alert_says_how_long_it_has_been_broken_not_just_how_often(bench):
    """"3 failed runs in a row" is the same sentence after six minutes and
    after six hours, and the two need different reactions."""
    bench.run()                                   # one good read to date from
    for _ in range(3):
        bench.run(fail=BlockedError("anti-bot page"))
    _, body = bench.sink.alerts[-1]
    assert "last read successfully" in body, body
    assert "hours" in body or "minutes" in body, body


def test_a_search_dark_for_long_enough_is_reported_before_the_third_failure(bench):
    """The threshold is a count, and the schedule is not evenly spaced.

    GitHub served 40% of the schedule asked of it here. Two failed checks can
    span most of a day, and a search unreadable all morning should not wait
    for a third firing that may not come.
    """
    bench.cfg.set("health.silent_after_hours", 6)
    bench.cfg.save()
    bench.run()
    bench.run(fail=BlockedError("anti-bot page"))
    assert not bench.sink.alerts_matching("needs attention"), \
        "one failure minutes ago is not yet a story"

    next_check(minutes=9 * 60)
    bench.run(fail=BlockedError("anti-bot page"))
    said = bench.sink.alerts_matching("needs attention")
    assert said, "two failures across nine dark hours is"
    assert "2 failed checks" in said[-1][1], said[-1][1]


def test_a_page_that_reads_as_nothing_counts_towards_the_alert(bench, fixture_html):
    """Each empty parse was first recorded as a good read, then as a failure,
    so the streak went 0 to 1 every run and the alert written for exactly
    this case never went out."""
    bench.run()
    good = json.loads((bench.path / "state.json").read_text())["searches"]
    (sid, before), = good.items()
    for _ in range(3):
        bench.run(search_html=fixture_html("search_unreadable"))
    health = json.loads((bench.path / "state.json").read_text())["searches"][sid]
    assert health["consecutive_failures"] == 3
    assert health["last_ok"] == before["last_ok"]
    assert health["last_count"] == before["last_count"], "the collapse guard keeps its measure"
    said = bench.sink.alerts_matching("needs attention")
    assert len(said) == 1 and "doctor --live" in said[0][1], said


def test_a_single_healthy_run_raises_no_alarm(bench):
    """A first run does report that it checked itself, but never as a problem."""
    bench.run()
    problems = [subject for subject, _ in bench.sink.alerts
                if "needs attention" in subject.lower() or "looks wrong" in subject.lower()]
    assert problems == []


def test_the_first_run_confirms_the_parser_works(bench):
    bench.run()
    subjects = [subject for subject, _ in bench.sink.alerts]
    assert any("is working" in s for s in subjects), subjects


def test_later_runs_do_not_repeat_the_first_run_check(bench):
    bench.run()
    bench.sink.alerts.clear()
    bench.run()
    assert bench.sink.alerts == []


def test_state_is_written_even_when_the_run_fails(bench):
    bench.run(fail=BlockedError("blocked"))
    saved = json.loads((bench.path / "state.json").read_text())
    assert saved["runs"], "the run must be recorded even after a failure"
    assert saved["runs"][0]["ok"] is False


def test_filtered_cars_are_never_announced(bench):
    bench.cfg.set("filters.max_price", 100500)
    report = bench.run()
    assert report.filtered_out >= 1
    for change in bench.sink.digests[0]:
        assert change.listing.price is None or change.listing.price <= 100500


def test_a_filtered_car_is_not_later_reported_as_removed(bench):
    """A car hidden by a filter is still present on the site."""
    bench.cfg.set("filters.max_price", 100500)
    bench.cfg.set("notifications.notify_on.removed", True)
    bench.run()
    bench.sink.digests.clear()
    for _ in range(3):
        report = bench.run()
    assert report.removed == 0


def test_a_hidden_car_that_goes_is_counted_not_announced(bench):
    """With removal alerts on, a car a rule hid - one you never heard of -
    was announced as removed."""
    bench.cfg.set("filters.max_price", 100000)       # hides the $102,199 car
    bench.cfg.set("notifications.notify_on.removed", True)
    bench.cfg.save()
    bench.run()
    bench.sink.digests.clear()
    head, *items = bench.cards.split('<div class="result-item">')
    without = head + "".join('<div class="result-item">' + item for item in items
                             if "_68819631_" not in item)
    events, removed = {}, 0
    for _ in range(3):
        report = bench.run(search_html=without)
        removed += report.removed
        for kind, count in report.hidden_events.items():
            events[kind] = events.get(kind, 0) + count
        assert not report.invariants, report.invariants
    sent = [(c.kind, c.listing.id) for d in bench.sink.digests for c in d]
    assert (Change.REMOVED, "68819631") not in sent, sent
    assert removed == 0 and events.get(Change.REMOVED) == 1
    entry = json.loads((bench.path / "state.json").read_text())["listings"]["68819631"]
    assert entry["status"] == "gone"
    assert entry["quiet_reason"].startswith("hidden by your rules")


def test_quiet_hours_hold_alerts_instead_of_dropping_them(bench, monkeypatch):
    monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: True)
    report = bench.run()
    assert report.quiet and bench.sink.digests == []
    assert report.new == 3

    monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: False)
    bench.run()
    assert bench.sink.digests, "held changes must arrive after quiet hours end"


def test_dry_run_changes_nothing_on_disk(bench):
    state_file = bench.path / "state.json"
    report = run(bench.cfg, State.load(state_file),
                 fetcher=FakeFetcher(bench.cards), dry_run=True)
    assert report.new == 3
    assert not state_file.exists()
    assert bench.sink.digests == []


def test_a_run_with_no_searches_explains_itself(bench):
    cfg = Config.defaults(bench.path / "empty.json")
    report = run(cfg, State.load(bench.path / "s2.json"), fetcher=FakeFetcher(""))
    assert "No searches configured" in " ".join(report.warnings)


def test_pagination_stops_once_a_page_adds_nothing(bench):
    bench.cfg.set("scraping.max_pages", 5)
    fetcher = FakeFetcher(bench.cards)
    run(bench.cfg, State.load(bench.path / "state.json"), fetcher=fetcher)
    search_pages = [u for u in fetcher.urls if "/cars/" in u]
    # Page 2 repeats page 1's cars, so the crawl stops there rather than
    # fetching all five pages.
    assert len(search_pages) == 2


def test_detail_pages_are_only_fetched_for_cars_we_have_not_seen(bench):
    bench.run()
    fetcher = FakeFetcher(bench.cards, {})
    run(bench.cfg, State.load(bench.path / "state.json"), fetcher=fetcher)
    assert not [u for u in fetcher.urls if "/a/" in u]


def test_an_alert_survives_every_channel_being_down(bench, monkeypatch):
    """If Telegram is down when a car appears, the car must still be announced
    once Telegram comes back - not silently skipped."""
    class Dead(Notifier):
        name = "dead"
        def _send(self, changes, run): raise RuntimeError("service unavailable")

    real = notifiers.dispatch
    monkeypatch.setattr(runner_mod.notifiers, "dispatch",
                        lambda c, ch, r=None, e=None, n=None: real(c, ch, r, e, [Dead({}, {}, {})]))
    report = bench.run()
    assert report.new == 3

    monkeypatch.setattr(runner_mod.notifiers, "dispatch",
                        lambda c, ch, r=None, e=None, n=None: real(c, ch, r, e, [bench.sink]))
    bench.run()
    assert bench.sink.digests, "the held alert must be retried"
    assert len(bench.sink.digests[0]) == 3


def test_a_held_alert_is_only_delivered_once(bench, monkeypatch):
    monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: True)
    bench.run()
    monkeypatch.setattr(runner_mod.notifiers, "in_quiet_hours", lambda *a, **k: False)
    bench.run()
    bench.sink.digests.clear()
    bench.run()
    assert bench.sink.digests == []


def test_a_stable_card_price_never_triggers_a_second_detail_fetch(bench):
    """A card and its listing page may disagree permanently; that must not cost
    one extra request per car per run."""
    bench.run()
    for _ in range(3):
        fetcher = FakeFetcher(bench.cards, {})
        run(bench.cfg, State.load(bench.path / "state.json"), fetcher=fetcher)
        assert not [u for u in fetcher.urls if "/a/" in u]


def test_a_changed_card_price_does_trigger_a_re_check(bench):
    bench.run()
    fetcher = FakeFetcher(bench.cards.replace("$98,995", "$91,000"), {})
    run(bench.cfg, State.load(bench.path / "state.json"), fetcher=fetcher)
    assert [u for u in fetcher.urls if "_13166607_" in u]


class TestFirstRunSelfCheck:
    def test_a_first_run_that_parses_garbage_records_nothing(self, bench, fixture_html):
        """Better an empty state and a loud complaint than archived debris."""
        report = bench.run(search_html=fixture_html("search_unreadable"))
        assert report.first_run
        assert not report.validation_ok
        assert not report.ok
        state = json.loads((bench.path / "state.json").read_text())
        assert state["listings"] == {}, "nothing may be recorded from a bad parse"

    def test_a_bad_first_run_says_so_loudly(self, bench, fixture_html):
        bench.run(search_html=fixture_html("search_unreadable"))
        subjects = [subject for subject, _ in bench.sink.alerts]
        assert any("looks wrong" in s for s in subjects), subjects

    def test_a_bad_first_run_leaves_a_report(self, bench, fixture_html):
        bench.run(search_html=fixture_html("search_unreadable"))
        report = (bench.path / "validation-report.md").read_text()
        assert "Something looks wrong" in report
        assert "Nothing was recorded" in report

    def test_a_long_outage_does_not_make_the_bot_new_again(self, bench):
        """The run log keeps sixty runs. A day and a half of failed firings
        pushed the last good check out of it, and the next good check was
        treated as the first: the self-check was sent again, and the guards
        that trust a proven parser were switched off."""
        from autotrader.state import MAX_RUN_HISTORY
        bench.run()
        for _ in range(MAX_RUN_HISTORY + 1):
            bench.run(fail=FetchError("connection reset"), minutes_later=30)
        saved = json.loads((bench.path / "state.json").read_text())
        assert not any(r.get("ok") for r in saved["runs"]), "the log forgot"
        bench.sink.alerts.clear()
        report = bench.run()
        assert not report.first_run
        assert not bench.sink.alerts_matching("is working")

    def test_a_firing_that_stood_down_is_not_a_check_that_worked(self, bench):
        """Recorded as ok, and it read nothing."""
        state = State.load(bench.path / "state.json")
        state.record_run({"ok": True, "skipped": True, "searches_run": 0})
        state.save()
        assert bench.run().first_run

    def test_a_good_first_run_records_normally(self, bench):
        report = bench.run()
        assert report.first_run and report.validation_ok and report.ok
        state = json.loads((bench.path / "state.json").read_text())
        assert len(state["listings"]) == 3

    def test_a_good_first_run_leaves_a_report_with_a_sample(self, bench):
        bench.run()
        report = (bench.path / "validation-report.md").read_text()
        assert "Looks right" in report
        assert "2021 BMW M5 Competition Sedan" in report
        assert "$98,995" in report

    def test_the_check_only_happens_until_a_run_succeeds(self, bench, fixture_html):
        bad = bench.run(search_html=fixture_html("search_unreadable"))
        assert bad.first_run, "a failed run does not count as having proved anything"
        good = bench.run()
        assert good.first_run and good.validation_ok
        later = bench.run()
        assert not later.first_run

    def test_a_recovered_parse_records_what_it_skipped_before(self, bench, fixture_html):
        bench.run(search_html=fixture_html("search_unreadable"))
        bench.sink.digests.clear()
        bench.run()
        delivered = {c.listing.id for batch in bench.sink.digests for c in batch}
        assert len(delivered) == 3

    def test_a_degraded_parse_is_allowed_once_the_parser_has_proved_itself(
            self, bench, fixture_html):
        """The gate guards the unproven first run, not every wobble afterwards."""
        bench.run()
        report = bench.run(search_html=fixture_html("search_broken_primary"))
        assert not report.first_run
        assert report.searches_run == 1


class TestSelfConfiguration:
    def test_a_bare_install_makes_itself_reachable(self, tmp_path, monkeypatch,
                                                   fixture_html, archive_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(SEARCH, "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.save()
        assert cfg.active_channels({}) == []

        report = run(cfg, State.load(tmp_path / "state.json"),
                     fetcher=FakeFetcher(fixture_html("search_cards")), env={})
        assert report.provisioned["channels"] == ["ntfy"]
        assert report.provisioned["subscribe_url"].startswith("https://ntfy.sh/")

    def test_setup_is_reported_so_the_user_can_see_it_happened(self, tmp_path, monkeypatch,
                                                               fixture_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(SEARCH, "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.save()
        report = run(cfg, State.load(tmp_path / "state.json"),
                     fetcher=FakeFetcher(fixture_html("search_cards")), env={})
        assert any("ntfy" in w for w in report.warnings)

    def test_a_dry_run_configures_nothing(self, tmp_path, monkeypatch, fixture_html):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search(SEARCH, "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.save()
        before = (tmp_path / "config.json").read_bytes()
        report = run(cfg, State.load(tmp_path / "state.json"),
                     fetcher=FakeFetcher(fixture_html("search_cards")), env={},
                     dry_run=True)
        # The same bare install as above, which a real run makes reachable by
        # choosing itself a topic. A dry run must not: no topic, no channel,
        # and the config on disk exactly as it was.
        assert not report.provisioned
        assert not cfg.get("notifications.channels.ntfy.topic")
        assert cfg.active_channels({}) == []
        assert (tmp_path / "config.json").read_bytes() == before


class TestTheFiguresTheRulesJudge:
    """A known car's listing page is read only when its card price moves, so
    between those reads the rules saw the card while state kept the page.
    The two disagree often, and a car either side of a rule's bound flipped:
    hidden on one sighting, "now within your rules" on the next."""

    @staticmethod
    def _entry(bench, lid):
        return json.loads((bench.path / "state.json").read_text())["listings"][lid]

    def test_a_car_its_listing_page_puts_over_the_ceiling_stays_hidden(self, bench):
        # Card $98,995, listing page $109,999.
        bench.cfg.set("filters.max_price", 100000)
        bench.cfg.save()
        for _ in range(3):
            bench.run()
            entry = self._entry(bench, "13166607")
            assert entry["filtered"], entry.get("filter_reason")
            assert entry["price"] == 109999
        sent = [(c.kind, c.listing.id) for d in bench.sink.digests for c in d]
        assert ("qualified", "13166607") not in sent, sent

    def test_a_car_its_listing_page_puts_inside_the_odometer_limit_stays_shown(self, bench):
        # Card 88,100 km, listing page 88,000 km.
        bench.cfg.set("filters.max_mileage_km", 88050)
        bench.cfg.save()
        for _ in range(3):
            bench.run()
            entry = self._entry(bench, "13221555")
            assert not entry["filtered"], entry.get("filter_reason")
            assert entry["mileage_km"] == 88000

    def test_a_drop_whose_listing_page_failed_once_is_still_announced(
            self, bench, archive_html):
        """The failed read left the new card figure stored, so the card looked
        settled and the page was never asked again: the drop was lost."""
        class FailsOnce(FakeFetcher):
            failed = []

            def get(self, url, referer=None, allow_block=False):
                if "_68819631_" in url and not FailsOnce.failed:
                    FailsOnce.failed.append(url)
                    self.urls.append(url)
                    raise FetchError(f"HTTP 503 from {url}", status=503)
                return super().get(url, referer, allow_block)

        bench.run()
        bench.sink.digests.clear()
        cards = bench.cards.replace("$102,199", "$94,000")
        cheaper = {i: archive_html(i) for i in ("13166607", "13221555")}
        cheaper["68819631"] = (archive_html("68819631")
                               .replace('"price":"102199"', '"price":"94000"')
                               .replace('"price": "102199"', '"price": "94000"'))
        asked, drops = [], 0
        for _ in range(4):
            next_check()
            fetcher = FailsOnce(cards, cheaper)
            drops += run(bench.cfg, State.load(bench.path / "state.json"),
                         fetcher=fetcher).price_drops
            asked.append(len([u for u in fetcher.urls if "_68819631_" in u]))
        assert drops == 1
        assert asked == [1, 1, 0, 0], "asked until it answered, then left alone"
        sent = [(c.kind, c.listing.id, c.new_price) for d in bench.sink.digests for c in d]
        assert sent == [(Change.PRICE_DROP, "68819631", 94000)], sent
        assert self._entry(bench, "68819631")["price"] == 94000


class TestAnotherSearchsRuleWaitsForTheOwner:
    """Two searches return the same cars and the second hides the dearer ones.
    When the car's own search missed a read, the second search's rule hid it
    under its own name, and the owner's next read announced it as "now
    within your rules" - about cars you had already been told of."""

    OWNER_MARK = "srt=35"          # only the first search's link carries it

    def _two_searches(self, bench):
        bench.cfg.add_search(
            "https://www.autotrader.ca/cars/honda/civic/?rcp=15&prx=250&loc=K1P",
            "Nearby and cheaper")
        bench.cfg.data["searches"][1]["filters"] = {"max_price": 50000}
        bench.cfg.save()

    def _run(self, bench, archive_html, owner_page=None, owner_fails=False):
        mark = self.OWNER_MARK

        class PerSearch(FakeFetcher):
            def get(self, url, referer=None, allow_block=False):
                if mark in url and owner_fails:
                    self.urls.append(url)
                    raise FetchError(f"HTTP 503 from {url}", status=503)
                if mark in url and owner_page is not None and "/cars/" in url:
                    self.urls.append(url)
                    from autotrader.http import Response
                    return Response(url=url, status=200, text=owner_page, elapsed_ms=1)
                return super().get(url, referer, allow_block)

        next_check()
        details = {i: archive_html(i) for i in ("13166607", "68819631", "13221555")}
        return run(bench.cfg, State.load(bench.path / "state.json"),
                   fetcher=PerSearch(bench.cards, details))

    def _owners(self, bench):
        saved = json.loads((bench.path / "state.json").read_text())["listings"]
        return {lid: (e["search_id"], e["filtered"]) for lid, e in saved.items()}

    def test_an_owner_that_failed_once_keeps_its_cars(self, bench, archive_html):
        self._two_searches(bench)
        self._run(bench, archive_html)
        dearer = ("13166607", "68819631")          # over the second search's ceiling
        before = {lid: self._owners(bench)[lid] for lid in dearer}
        bench.sink.digests.clear()
        self._run(bench, archive_html, owner_fails=True)
        assert {lid: self._owners(bench)[lid] for lid in dearer} == before
        report = self._run(bench, archive_html)
        sent = [(c.kind, c.listing.id) for d in bench.sink.digests for c in d]
        assert not [s for s in sent if s[0] == Change.QUALIFIED], sent
        assert report.qualified == 0

    def test_an_owner_that_read_only_part_of_its_results_keeps_its_cars(
            self, bench, archive_html):
        self._two_searches(bench)
        bench.cfg.data["searches"][0]["max_pages"] = 1      # never reads to the end
        bench.cfg.save()
        self._run(bench, archive_html)
        before = self._owners(bench)
        bench.sink.digests.clear()
        head, *items = bench.cards.split('<div class="result-item">')
        sample = head + "".join('<div class="result-item">' + item for item in items
                                if "_13166607_" not in item)
        self._run(bench, archive_html, owner_page=sample)
        assert self._owners(bench)["13166607"] == before["13166607"]
        self._run(bench, archive_html)
        sent = [(c.kind, c.listing.id) for d in bench.sink.digests for c in d]
        assert not [s for s in sent if s[0] == Change.QUALIFIED], sent

    def test_an_owner_that_read_everything_still_hands_a_car_over(self, bench, archive_html):
        """Read to the end and the car was not there: the other search's
        rule is the only word on it."""
        self._two_searches(bench)
        self._run(bench, archive_html)
        head, *items = bench.cards.split('<div class="result-item">')
        without = head + "".join('<div class="result-item">' + item for item in items
                                 if "_13166607_" not in item)
        self._run(bench, archive_html, owner_page=without)
        owner, hidden = self._owners(bench)["13166607"]
        assert hidden and owner == bench.cfg.searches[1].id
