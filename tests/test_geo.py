"""Enforcing the location the pasted link asks for and the site ignores.

A pasted link can carry `prx=-1&loc=K1P+1J1`, and the 2026 platform drops
it: such a watch returns cars from provinces away. Nothing fails; the
constraint is simply not applied by anyone.
"""
import pytest

from autotrader import geo
from autotrader.filters import apply, check
from autotrader.listing import Listing

HERE = geo.locate_reference("K1P 1J1")
NEAR_HERE = {"near": "K1P 1J1", "max_distance_km": 300}


def car(city, province, **kw):
    return Listing(id=kw.pop("id", city or "x"), title="2022 Honda Civic",
                   price=30000, location=city, province=province, **kw)


class TestPlacingACar:
    def test_a_postal_code_resolves_to_its_neighbourhood(self):
        assert HERE is not None and HERE == geo.FSA["K1P"]
        assert geo.distance_km(HERE, geo.locate("Ottawa", "ON")) < 10

    def test_a_postal_code_with_no_entry_falls_back_to_its_region(self):
        assert geo.locate_reference("K9Z 1A1") == geo.POSTAL_ANCHOR["K"]
        assert geo.region_of("K9Z 1A1") == "ON"

    def test_a_city_and_province_resolve(self):
        assert geo.locate_reference("Toronto, ON") == geo.CITIES["ON"]["toronto"]

    def test_accents_and_punctuation_do_not_matter(self):
        assert geo.locate("Montréal", "QC") == geo.locate("MONTREAL", "QC")
        assert geo.locate("St. Catharines", "ON") == geo.locate("St catharines", "ON")
        assert geo.locate("Trois-Rivières", "QC") is not None

    def test_a_place_nobody_has_heard_of_is_simply_unknown(self):
        assert geo.locate("Nowheresville", "ON") is None

    def test_distances_are_the_right_order_of_magnitude(self):
        tor, cal = geo.locate("Toronto", "ON"), geo.locate("Calgary", "AB")
        assert 2600 < geo.distance_km(tor, cal) < 2800      # ~2,710 km
        ott = geo.locate("Ottawa", "ON")
        assert geo.distance_km(ott, geo.locate("Gatineau", "QC")) < 20


class TestTheRadius:
    @pytest.mark.parametrize("city,province", [
        ("Ottawa", "ON"), ("Kanata", "ON"), ("Gatineau", "QC"),
        ("Cornwall", "ON"), ("Kingston", "ON"), ("Montréal", "QC"),
        ("Laval", "QC"),
    ])
    def test_the_city_and_the_places_around_it_are_in(self, city, province):
        assert check(car(city, province), NEAR_HERE).keep

    @pytest.mark.parametrize("city,province", [
        ("Toronto", "ON"), ("Thornhill", "ON"), ("Calgary", "AB"),
        ("Edmonton", "AB"), ("Sudbury", "ON"), ("Québec", "QC"),
        ("Winnipeg", "MB"), ("Saskatoon", "SK"), ("Halifax", "NS"),
        ("Vancouver", "BC"),
    ])
    def test_everything_beyond_the_radius_is_out(self, city, province):
        verdict = check(car(city, province), NEAR_HERE)
        assert not verdict.keep
        assert "beyond the 300 km" in verdict.reason

    def test_the_reason_names_the_distance(self):
        verdict = check(car("Toronto", "ON"), NEAR_HERE)
        assert "352 km" in verdict.reason and "K1P 1J1" in verdict.reason


class TestNotHidingWhatItCannotJudge:
    def test_a_car_with_no_location_is_kept(self):
        assert check(car("", ""), NEAR_HERE).keep

    def test_an_unknown_town_in_range_is_kept(self):
        """The table is the thing most likely to be incomplete, so a missing
        town must never be the reason a match is hidden."""
        assert check(car("Nowheresville", "ON"), NEAR_HERE).keep

    def test_an_unknown_town_in_a_province_nothing_can_reach_is_excluded(self):
        """Nothing in Alberta is within 300 km of Ottawa, whatever it is
        called, so this one can be decided honestly."""
        assert not check(car("Nowheresville", "AB"), NEAR_HERE).keep

    def test_a_reference_that_cannot_be_placed_enforces_nothing(self):
        rule = {"near": "Narnia", "max_distance_km": 50}
        assert check(car("Toronto", "ON"), rule).keep

    def test_no_radius_means_no_rule(self):
        assert check(car("Toronto", "ON"), {"near": "K1P 1J1"}).keep
        assert check(car("Toronto", "ON"), {"near": "K1P 1J1",
                                            "max_distance_km": 0}).keep


class TestProvinceAllowlist:
    def test_it_keeps_only_the_listed_provinces(self):
        kept, _, dropped, _wrong = apply(
            [car("Ottawa", "ON", id="a"), car("Calgary", "AB", id="b")],
            {"provinces": ["ON"]})
        assert [l.id for l in kept] == ["a"]
        assert "not one of ON" in dropped[0][1].reason
        assert dropped[0][1].rule == "provinces"

    def test_a_car_with_no_province_is_kept(self):
        assert check(car("Somewhere", ""), {"provinces": ["ON"]}).keep


class TestWhatTheDashboardIsTold:
    def test_the_area_is_spelled_out(self):
        from autotrader.dashboard import _area_of
        area = _area_of(NEAR_HERE)
        assert area["text"] == "within 300 km of K1P 1J1"
        assert area["resolved"] is True and area["province"] == "ON"

    def test_an_unresolvable_reference_says_so(self):
        from autotrader.dashboard import _area_of
        assert _area_of({"near": "Narnia", "max_distance_km": 50})["resolved"] is False

    def test_no_rule_means_nothing_to_show(self):
        from autotrader.dashboard import _area_of
        assert _area_of({"max_price": 50000}) is None


class TestAWatchThatMatchesNothing:
    """Reading the site fine and keeping nothing is not a failure, but it
    looks exactly like one - and a rule quietly matching nothing for months
    is the reason to say so out loud.

    A tight radius around a place none of the results are near does exactly
    this: the search reads fine and every car is turned away.
    """

    def test_it_says_what_turned_everything_away(self, tmp_path, monkeypatch,
                                                 fixture_html):
        from autotrader import runner as runner_mod
        from autotrader.config import Config
        from autotrader.runner import run
        from autotrader.state import State
        from .helpers import Capture, FakeFetcher, use_channels

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25",
                       "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        # Tight enough that no car in the payload is inside it, so every one
        # of the nineteen is turned away by distance.
        cfg.set("filters.near", "K1P 1J1")
        cfg.set("filters.max_distance_km", 3)
        cfg.save()
        use_channels(monkeypatch, runner_mod, [Capture()])

        report = run(cfg, State.load(tmp_path / "state.json"),
                     fetcher=FakeFetcher(fixture_html("search_next_data")), env={})

        assert report.ok, report.errors
        assert report.shut_out == ["Example search"]
        warning = next(w for w in report.warnings if "none passed" in w)
        assert "19 too far away" in warning
        assert "It is working" in warning

    def test_the_reason_ranks_the_rules_that_hurt_most(self):
        from autotrader.runner import _why_none_survived
        from autotrader.listing import Listing

        car = Listing(id="x", title="t")
        from autotrader.filters import Verdict
        dropped = [(car, Verdict(False, "Toronto, ON is 352 km from K1P 1J1, "
                                        "beyond the 300 km you asked for",
                                 rule="max_distance_km"))] * 17
        dropped += [(car, Verdict(False, "year 2016 below minimum 2018",
                                  rule="min_year"))] * 2
        assert _why_none_survived(dropped) == "17 too far away, 2 outside the year range"

    def test_a_search_that_finds_something_again_clears_the_marker(
            self, tmp_path, monkeypatch, fixture_html):
        from autotrader import runner as runner_mod
        from autotrader.config import Config
        from autotrader.runner import run
        from autotrader.state import State
        from .helpers import Capture, FakeFetcher, use_channels

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25",
                       "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("filters.provinces", ["NU"])
        cfg.save()
        use_channels(monkeypatch, runner_mod, [Capture()])
        html = fixture_html("search_next_data")

        run(cfg, State.load(tmp_path / "state.json"),
            fetcher=FakeFetcher(html), env={})
        state = State.load(tmp_path / "state.json")
        sid = cfg.searches[0].id
        assert state.search_health(sid).get("shut_out")

        cfg.set("filters.provinces", [])
        cfg.save()
        run(cfg, State.load(tmp_path / "state.json"),
            fetcher=FakeFetcher(html), env={})
        assert not State.load(tmp_path / "state.json").search_health(sid).get("shut_out")

    def test_a_search_that_keeps_something_is_not_flagged(self, tmp_path,
                                                          monkeypatch, fixture_html):
        """Three of these nineteen are within reach: two in Montreal, and one
        in a Quebec town the table cannot place, which is kept rather than
        guessed at."""
        from autotrader import runner as runner_mod
        from autotrader.config import Config
        from autotrader.runner import run
        from autotrader.state import State
        from .helpers import Capture, FakeFetcher, use_channels

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25",
                       "Example search")
        cfg.set("scraping.delay_ms", 0)
        cfg.set("scraping.enrich_details", False)
        cfg.set("archive.mode", "off")
        cfg.set("filters.near", "K1P 1J1")
        cfg.set("filters.max_distance_km", 300)
        cfg.save()
        use_channels(monkeypatch, runner_mod, [Capture()])

        report = run(cfg, State.load(tmp_path / "state.json"),
                     fetcher=FakeFetcher(fixture_html("search_next_data")), env={})
        assert report.shut_out == []
        assert report.new == 3
