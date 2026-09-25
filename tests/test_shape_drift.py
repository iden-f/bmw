"""Early warning that the site has changed shape, before the bot goes dark."""
import json


from autotrader.shape import compare, describe, fingerprint



def shot(html="<html></html>", strategy="jsonld", scores=None, count=10):
    return fingerprint(html, strategy,
                       scores or {"jsonld": count, "embedded_json": count,
                                  "anchors": count, "regex": count},
                       count)


class TestComparison:
    def test_an_unchanged_page_is_not_drift(self):
        one = shot()
        assert compare(one, one) == ([], False)

    def test_a_first_sighting_is_recorded_but_not_alarming(self):
        reasons, serious = compare(None, shot())
        assert reasons and not serious

    def test_a_changed_winner_is_serious(self):
        before = shot(strategy="jsonld")
        after = shot(strategy="regex")
        reasons, serious = compare(before, after)
        assert serious
        assert "'regex' instead of 'jsonld'" in " ".join(reasons)

    def test_losing_a_strategy_is_noted(self):
        before = shot(scores={"jsonld": 9, "anchors": 9, "regex": 9})
        after = shot(scores={"jsonld": 9, "regex": 9})
        reasons, serious = compare(before, after)
        assert "anchors stopped working" in " ".join(reasons)
        assert not serious, "two strategies left is a note, not an emergency"

    def test_losing_the_last_fallback_is_serious(self):
        before = shot(scores={"jsonld": 9, "anchors": 9})
        after = shot(scores={"jsonld": 9})
        _, serious = compare(before, after)
        assert serious

    def test_a_recovered_strategy_is_reported_but_calm(self):
        before = shot(scores={"jsonld": 9})
        after = shot(scores={"jsonld": 9, "anchors": 9})
        reasons, serious = compare(before, after)
        assert "started working again" in " ".join(reasons)
        assert not serious

    def test_a_vanished_structural_marker_is_serious(self):
        base = shot('<script type="application/ld+json">{}</script>')
        after = shot("<html>no structured data</html>")
        reasons, serious = compare(base, after)
        assert serious
        assert "json_ld" in " ".join(reasons)

    def test_a_collapse_in_results_is_serious(self):
        before = shot(count=20)
        after = shot(count=3)
        reasons, serious = compare(before, after)
        assert serious
        assert "fell from 20 to 3" in " ".join(reasons)

    def test_ordinary_day_to_day_variation_is_not_drift(self):
        """A results page has a different number of cars every day."""
        before = shot(count=19)
        after = shot(count=17)
        assert compare(before, after) == ([], False)

    def test_a_small_search_is_not_judged_on_counts(self):
        before = shot(count=3)
        after = shot(count=1)
        _, serious = compare(before, after)
        assert not serious

    def test_the_description_is_readable(self):
        reasons, _ = compare(shot(strategy="jsonld"), shot(strategy="regex"))
        text = describe("Example search", reasons, shot(strategy="regex"))
        assert "Example search" in text and "regex" in text
        assert "Nothing is broken yet" in text


class TestFingerprint:
    def test_the_real_platform_page_is_recognised(self, fixture_html):
        from autotrader.parser import parse_search_page
        html = fixture_html("search_2026_platform")
        result = parse_search_page(html, "https://www.autotrader.ca/cars/honda/civic")
        print = fingerprint(html, result.strategy, result.candidates, len(result.listings))
        assert print["markers"]["json_ld"] != "none"
        assert print["markers"]["offer_links"] != "none"
        assert print["markers"]["legacy_links"] == "none"

    def test_the_old_platform_page_looks_different(self, fixture_html):
        old = fingerprint(fixture_html("search_cards"), "anchors",
                          {"anchors": 3, "regex": 3}, 3)
        new = fingerprint(fixture_html("search_2026_platform"), "jsonld",
                          {"jsonld": 3, "regex": 3}, 3)
        reasons, serious = compare(old, new)
        assert serious, "a platform migration must not pass quietly"


class TestInTheRunner:
    def test_a_baseline_is_recorded_on_the_first_run(self, bench):
        bench.run()
        state = json.loads((bench.path / "state.json").read_text())
        shape = next(iter(state["searches"].values()))["shape"]
        assert shape["strategy"] == "anchors"
        assert shape["listing_count"] == 3

    def test_a_stable_site_produces_no_warnings(self, bench):
        bench.run()
        bench.sink.alerts.clear()
        bench.run()
        subjects = [s for s, _ in bench.sink.alerts]
        assert not any("changed how its pages" in s for s in subjects)

    def test_a_platform_change_warns_before_it_breaks(self, bench, fixture_html):
        """Still finding cars, but by a different route - the warning window."""
        bench.run()
        bench.sink.alerts.clear()
        report = bench.run(search_html=fixture_html("search_2026_platform"))
        assert report.shape_drift
        subjects = [s for s, _ in bench.sink.alerts]
        assert any("changed how its pages are built" in s for s in subjects)

    def test_the_page_is_captured_when_the_shape_moves(self, bench, fixture_html):
        bench.run()
        report = bench.run(search_html=fixture_html("search_2026_platform"))
        assert report.diagnostics
        captured = (bench.path / "diagnostics").glob("*.md")
        assert any("What the scraper saw" in p.read_text() for p in captured)

    def test_the_first_run_captures_a_baseline_snapshot(self, bench):
        """A baseline is only useful if we know what it looked like."""
        report = bench.run()
        assert report.diagnostics

    def test_drift_watching_can_be_switched_off(self, bench):
        bench.cfg.set("health.watch_page_shape", False)
        report = bench.run()
        assert report.shape_drift == []
