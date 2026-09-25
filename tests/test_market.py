"""The market layer, and how honest it is about how little it knows.

Two hundred listings over a few days is a snapshot, not a time series. The
danger in this whole feature is a number that reads like a trend - "median
asking up $1,200" - computed from ten cars seen twice. Every test here is
about the numbers being right, or about them refusing to be stated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autotrader import insight

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat(timespec="seconds")


def runs_since(days: float) -> list[dict]:
    """A run log whose oldest entry is where the watch began."""
    return [{"at": ago(days)}, {"at": ago(days / 2)}, {"at": ago(0)}]


def car(i, *, price=90000, year=2019, trim="", status="active",
        first=30, removed=None, history=None, title="",
        make="", model="", filtered=False):
    entry = {
        "id": f"{i:08d}-0000-0000-0000-000000000000",
        "price": price, "year": year, "trim": trim, "title": title,
        "make": make, "model": model, "filtered": filtered,
        "status": status, "first_seen": ago(first),
        "price_history": history if history is not None
        else [{"at": ago(first), "price": price}],
    }
    if removed is not None:
        entry["removed_at"] = ago(removed)
    return entry


class TestPriceByYear:
    def test_a_year_with_enough_cars_gets_a_median(self):
        cars = [car(i, year=2019, price=p) for i, p in
                enumerate([60000, 70000, 80000, 90000, 100000])]
        out = insight.market(cars, now=NOW)
        assert out["by_year"]["2019"]["n"] == 5
        assert out["by_year"]["2019"]["median"] == 80000
        assert out["by_year"]["2019"]["low"] == 60000

    def test_a_year_with_two_cars_shows_the_two_cars(self):
        """Two cars is not a median, it is two cars - so it says which two.

        This used to drop the row entirely, which is honest and useless: on a
        watch holding five cars, dropping every thin row leaves an empty tab.
        Below the threshold the prices themselves are published instead.
        """
        out = insight.market([car(1, year=2021, price=60000),
                              car(2, year=2021, price=70000)], now=NOW)
        row = out["by_year"]["2021"]
        assert row["thin"] is True and row["n"] == 2
        assert row["prices"] == [60000, 70000]
        assert "median" not in row

    def test_trims_are_grouped_from_the_words_that_move_the_price(self):
        cars = ([car(i, title="Example Coupe Competition AWD", price=45000)
                 for i in range(5)]
                + [car(10 + i, title="Example Coupe Touring", price=30000)
                   for i in range(5)])
        out = insight.market(cars, now=NOW)
        assert out["by_trim"]["competition"]["median"] == 45000
        assert out["by_trim"]["touring"]["median"] == 30000


class TestHowLongThingsLast:
    def test_it_measures_time_listed_not_time_to_sell(self):
        cars = [car(i, status="gone", first=40, removed=10) for i in range(6)]
        out = insight.market(cars, now=NOW)
        assert out["listed_days"]["median"] == 30
        assert "not how long it took to sell" in out["listed_days"]["note"]

    def test_cars_still_up_are_counted_separately(self):
        out = insight.market([car(i, first=12) for i in range(5)], now=NOW)
        assert out["still_listed_days"]["median"] == 12
        assert out["listed_days"]["n"] == 0


class TestSayingHowLittleItKnows:
    def test_a_thin_window_is_flagged_and_explained(self):
        out = insight.market([car(i, first=2) for i in range(50)], now=NOW)
        assert out["window"]["thin"] is True
        assert out["window"]["cars_with_two_prices"] == 0
        assert "snapshot" in out["window"]["note"]

    def test_a_long_window_with_real_history_is_not_flagged(self):
        cars = [car(i, first=90, history=[{"at": ago(90), "price": 90000},
                                          {"at": ago(10), "price": 88000}])
                for i in range(25)]
        out = insight.market(cars, now=NOW)
        assert out["window"]["thin"] is False
        assert out["window"]["cars_with_two_prices"] == 25


class TestCompaction:
    def test_recent_points_are_all_kept(self):
        history = [{"at": ago(d), "price": 90000 - d} for d in range(20, 0, -1)]
        out = insight.compact_history(history, now=NOW)
        assert out == history

    def test_old_points_are_thinned_to_one_a_week(self):
        """Thirty days kept daily, then one a week. Roughly a quarter."""
        history = [{"at": ago(d), "price": 90000} for d in range(300, 0, -1)]
        out = insight.compact_history(history, now=NOW)
        daily = insight.KEEP_DAILY_DAYS
        weekly = (300 - daily) / 7
        assert daily <= len(out) <= daily + weekly + 4, len(out)
        assert len(out) < len(history) / 3

    def test_the_endpoints_always_survive(self):
        """Losing one moves the numbers the history exists to give you."""
        history = [{"at": ago(d), "price": 100000 + d} for d in range(400, 0, -1)]
        out = insight.compact_history(history, now=NOW)
        assert out[0] == history[0]
        assert out[-1] == history[-1]

    def test_a_short_history_is_left_alone(self):
        history = [{"at": ago(300), "price": 1}, {"at": ago(1), "price": 2}]
        assert insight.compact_history(history, now=NOW) == history


class TestTheScoreIsTested:
    def test_it_refuses_a_verdict_on_too_few_cars(self):
        out = insight.backtest([car(i) for i in range(4)])
        assert "not enough scored cars" in out["verdict"]

    def test_it_says_plainly_when_the_score_predicts_nothing(self):
        """A score nobody has checked is decoration."""
        cars = []
        # Ten cheap ones that all cut, ten dear ones that never do: the
        # opposite of what the score claims.
        for i in range(10):
            cars.append(car(i, price=60000, year=2019, title="Honda Civic",
                            history=[{"at": ago(20), "price": 70000},
                                     {"at": ago(1), "price": 60000}]))
        for i in range(10):
            cars.append(car(100 + i, price=140000, year=2019, title="Honda Civic",
                            history=[{"at": ago(20), "price": 140000}]))
        for entry in cars:
            entry["make"], entry["model"] = "Honda", "Civic"
        out = insight.backtest(cars)
        assert out["called_cheap"] and out["called_dear"]
        assert "not predicting anything" in out["verdict"], out


class TestNotConfusingTheWatchWithTheMarket:
    """The failure this class exists to prevent, in one sentence.

    A bot that started watching on Tuesday reports that every car on the
    market arrived this week, that the median car has been listed two days,
    and that listings come down within a day of going up. All three are
    arithmetically correct and all three are statements about the bot. The
    market layer has to hand the page enough to say so.
    """

    def test_every_car_still_here_arrived_when_the_watch_did(self):
        """Right-censored: the figure is a floor, not a measurement."""
        fleet = [car(i, first=2) for i in range(30)]
        out = insight.market(fleet, now=NOW, runs=runs_since(2))
        assert out["still_listed_days"]["median"] == 2
        assert out["still_listed_days"]["censored"] is True
        assert out["still_listed_days"]["watching_days"] == 2

    def test_a_watch_older_than_its_oldest_car_is_measuring_the_market(self):
        """The bug this replaced: comparing a number against itself.

        Deriving "how long have we been watching" from the oldest car makes
        the censoring test read ``longest >= longest``, which is true for
        every dataset ever collected. The watch's own start comes from the
        run log, which is the only record of it.
        """
        fleet = [car(i, first=3) for i in range(10)]
        out = insight.market(fleet, now=NOW, runs=runs_since(200))
        assert out["still_listed_days"]["censored"] is False
        assert out["still_listed_days"]["watching_days"] == 200

    def test_with_no_run_log_it_falls_back_rather_than_crashing(self):
        fleet = [car(i, first=2) for i in range(5)]
        out = insight.market(fleet, now=NOW)
        assert out["still_listed_days"]["censored"] is True

    def test_a_young_watch_says_its_arrivals_are_not_a_week(self):
        out = insight.market([car(i, first=2) for i in range(5)], now=NOW,
                             runs=runs_since(2))
        assert out["velocity"]["window_is_the_watch"] is True

    def test_a_watch_older_than_a_week_means_this_week_means_this_week(self):
        fleet = [car(i, first=40) for i in range(5)]
        out = insight.market(fleet, now=NOW, runs=runs_since(90))
        assert out["velocity"]["window_is_the_watch"] is False
        assert out["velocity"]["arrived_7d"] == 0

    def test_only_short_lives_can_finish_inside_a_short_watch(self):
        """Length-biased sampling, flagged rather than silently reported."""
        fleet = [car(i, first=2) for i in range(10)]
        fleet += [car(50 + i, first=2, removed=1, status="gone")
                  for i in range(3)]
        out = insight.market(fleet, now=NOW, runs=runs_since(2))
        assert out["listed_days"]["n"] == 3
        assert out["listed_days"]["biased_short"] is True

    def test_a_long_watch_drops_the_warning(self):
        fleet = [car(i, first=400) for i in range(10)]
        fleet += [car(50 + i, first=300, removed=100, status="gone")
                  for i in range(3)]
        out = insight.market(fleet, now=NOW, runs=runs_since(400))
        assert out["listed_days"]["biased_short"] is False


class TestWordsAPersonWouldWrite:
    def test_one_day_is_not_one_days(self):
        assert insight._days(1) == "1 day"
        assert insight._days(2) == "2 days"
        assert insight._days(0) == "0 days"

    def test_no_programmer_pluralisation_reaches_the_page(self):
        out = insight.market([car(i, first=1) for i in range(3)], now=NOW,
                             runs=runs_since(1))
        assert "(s)" not in out["window"]["note"], out["window"]["note"]


class TestTheWatchStartSurvivesTheRunLog:
    """The run log is a rolling sixty; the watch start is not.

    At eighteen checks a day the log holds three days. A bot that reads its
    own start date off the end of that window reports "watching for 3 days"
    on its first anniversary, and every censored figure downstream stays
    wrong forever while looking freshly computed.
    """

    def test_the_first_run_writes_it_and_later_runs_leave_it_alone(self):
        from autotrader.state import State
        st = State()
        st.record_run({"ok": True})
        first = st.data["watch_started"]
        assert first
        st.record_run({"ok": True})
        assert st.data["watch_started"] == first

    def test_an_old_state_with_no_marker_falls_back_to_the_log(self):
        from autotrader.state import State
        st = State({"version": 2, "runs": [{"at": ago(9)}, {"at": ago(1)}]})
        assert st.watch_started == ago(9)

    def test_a_rolled_over_log_does_not_move_the_start(self):
        from autotrader.state import State
        st = State({"version": 2, "watch_started": ago(400),
                    "runs": [{"at": ago(1)}]})
        out = insight.market([car(1, first=1)], now=NOW, runs=st.runs,
                             since=st.watch_started)
        assert out["still_listed_days"]["watching_days"] == 400
        assert out["still_listed_days"]["censored"] is False
        assert out["velocity"]["window_is_the_watch"] is False

    def test_without_the_marker_the_same_state_would_have_lied(self):
        """The regression, stated as the difference it makes."""
        out = insight.market([car(1, first=1)], now=NOW, runs=[{"at": ago(1)}])
        assert out["still_listed_days"]["watching_days"] == 1


class TestThreeModelsInsteadOfOne:
    """A watch of three different cars, and a market layer that averaged
    across them.

    Output like "2017: median $30,000, from $12,000 to $45,000" puts a 2017
    Civic and a 2017 Corolla in one median, and a trim table reading
    "touring: 23 cars" is every model's Touring, and cars the year rule
    hides, together.
    """

    def fleet(self):
        return (
            [car(i, title="Honda Civic Touring", make="Honda", model="Civic",
                 year=2018, price=30000 + i * 1000) for i in range(6)]
            + [car(20 + i, title="Toyota Corolla", make="Toyota", model="Corolla",
                   year=2017, price=25000 + i * 1000) for i in range(2)]
            + [car(40 + i, title="Example Coupe", make="Example", model="Coupe",
                   year=2020, price=20000 + i * 1000) for i in range(5)]
        )

    def test_each_model_is_its_own_market(self):
        out = insight.market(self.fleet(), now=NOW)
        assert set(out["by_model"]) == {"Honda Civic", "Toyota Corolla", "Example Coupe"}

    def test_a_model_median_is_only_that_model(self):
        out = insight.market(self.fleet(), now=NOW)
        civic = out["by_model"]["Honda Civic"]
        assert civic["n"] == 6
        assert civic["low"] == 30000 and civic["high"] == 35000, \
            "another model's prices leaked into this one"

    def test_a_model_with_too_few_shows_the_cars_not_a_median(self):
        out = insight.market(self.fleet(), now=NOW)
        corolla = out["by_model"]["Toyota Corolla"]
        assert corolla["thin"] is True and corolla["prices"] == [25000, 26000]
        assert "median" not in corolla

    def test_the_flat_tables_follow_the_model_with_the_most_cars(self):
        out = insight.market(self.fleet(), now=NOW)
        assert out["leader"] == "Honda Civic"
        assert set(out["by_year"]) == {"2018"}, \
            "the flat by_year table is still mixing models"

    def test_every_row_carries_its_sample_size(self):
        out = insight.market(self.fleet(), now=NOW)
        for model in out["by_model"].values():
            assert "n" in model
            for row in list(model["by_year"].values()) + list(model["by_trim"].values()):
                assert "n" in row and row["n"] >= 1

    def test_no_row_shows_a_median_from_fewer_than_five(self):
        out = insight.market(self.fleet(), now=NOW)
        for model in out["by_model"].values():
            rows = [model] + list(model["by_year"].values()) + list(model["by_trim"].values())
            for row in rows:
                if "median" in row:
                    assert row["n"] >= insight.MIN_FOR_A_MEDIAN, row


class TestTheHiddenCarsAreNotTheMarket:
    """A watch for a Civic up to 2019 that reports the median of the 2025s
    its own rule rejects is quoting a market nobody is shopping in."""

    def fleet(self):
        yours = [car(i, make="Honda", model="Civic", year=2018, price=30000 + i * 1000)
                 for i in range(5)]
        theirs = [car(50 + i, make="Honda", model="Civic", year=2025, price=60000,
                      filtered=True) for i in range(9)]
        return yours + theirs

    def test_the_median_is_of_the_cars_you_could_buy(self):
        out = insight.market(self.fleet(), now=NOW)
        civic = out["by_model"]["Honda Civic"]
        assert civic["n"] == 5 and civic["median"] == 32000
        assert civic["high"] == 34000, "a hidden car is in the spread"

    def test_the_hidden_ones_are_still_counted_beside_it(self):
        out = insight.market(self.fleet(), now=NOW)
        assert out["by_model"]["Honda Civic"]["hidden"] == 9

    def test_a_model_that_is_entirely_hidden_still_gets_a_line(self):
        """Silently omitting a whole car you are searching for is worse than
        saying "9 of these, all hidden"."""
        out = insight.market([car(50 + i, make="Example", model="Coupe", year=2025,
                                  price=60000, filtered=True) for i in range(9)],
                             now=NOW)
        row = out["by_model"]["Example Coupe"]
        assert row["n"] == 0 and row["hidden"] == 9

    def test_a_car_is_scored_against_cars_you_could_buy(self):
        """comparables() had the same fault: a 2018 Civic judged against 2025
        Civics, which the year rule exists to exclude."""
        out = insight.comparables(self.fleet())
        for row in out.values():
            if "median" in row:
                assert row["median"] < 50000, "a hidden car is in the cohort"


class TestMeasuringTheRightWatch:
    """"watch_started" is when the BOT first ran. That stops being the same
    thing as "how long have we been watching these cars" the moment the watch
    list changes: a bot that has run for three days and then has its searches
    swapped for different cars has watched those cars for none of them."""

    def test_the_searches_first_read_beats_the_bots_own_birthday(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.data["watch_started"] = "2026-01-05T08:00:00+00:00"
        state.data["searches"] = {"a": {"first_ok": "2026-01-08T08:00:00+00:00"}}
        assert state.watching_these_since == "2026-01-08T08:00:00+00:00"

    def test_an_older_search_keeps_the_older_date(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.data["watch_started"] = "2026-01-05T08:00:00+00:00"
        state.data["searches"] = {"a": {"first_ok": "2026-01-06T00:00:00+00:00"},
                                  "b": {"first_ok": "2026-01-08T08:00:00+00:00"}}
        assert state.watching_these_since == "2026-01-06T00:00:00+00:00"

    def test_no_search_history_falls_back_to_the_bot(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.data["watch_started"] = "2026-01-05T08:00:00+00:00"
        assert state.watching_these_since == "2026-01-05T08:00:00+00:00"

    def test_first_ok_is_stamped_once_and_never_moves(self, tmp_path):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.record_search_ok("a", 5, "jsonld")
        first = state.data["searches"]["a"]["first_ok"]
        state.record_search_ok("a", 6, "jsonld")
        assert state.data["searches"]["a"]["first_ok"] == first
        assert state.data["searches"]["a"]["last_ok"] >= first

    def test_a_three_hour_watch_does_not_report_zero_days(self):
        """"over 0 days of watching" reads as a rounding error rather than
        the true and useful "the searches were read this morning"."""
        assert insight._span(3.4) == "3 hours"
        assert insight._span(0.2) == "12 minutes"
        assert insight._span(72) == "3 days"


class TestTheDenominatorCanMove:
    """A rate whose denominator holds cases that could not have gone the other
    way is not a rate. Every one of 83 cars first read this morning counted as
    a car that "did not cut its price"."""

    def watched_for(self, i, hours, **kw):
        from datetime import timedelta
        base = dict(price=60000, year=2019, make="Honda", model="Civic")
        base.update(kw)
        entry = car(i, **base)
        entry["first_seen"] = (NOW - timedelta(hours=hours)).isoformat()
        entry["last_seen"] = NOW.isoformat()
        return entry

    def test_a_car_read_this_morning_is_not_in_the_denominator(self):
        cars = [self.watched_for(i, 3) for i in range(12)]
        out = insight.backtest(cars)
        assert out["called_cheap"] == 0 and out["called_dear"] == 0

    def test_a_car_watched_for_days_is(self):
        cars = ([self.watched_for(i, 24 * 9, price=50000) for i in range(8)]
                + [self.watched_for(50 + i, 24 * 9, price=90000) for i in range(8)])
        out = insight.backtest(cars)
        assert out["called_cheap"] + out["called_dear"] > 0


class TestEveryCarGetsAnAnswer:
    """A blank meant five different things, and only one of them was
    "we compared it and there was nothing to say".

    comparables() used to key its result on the pool - priced, dated,
    named, live, not hidden by a rule - so a car failing any of those
    five had no row at all, and the page rendered the same nothing it
    renders for a car with a cohort of two.
    """

    @staticmethod
    def _out(*cars):
        return insight.comparables(list(cars))

    def test_a_car_with_no_price_says_so(self):
        out = self._out(car(1, price=None, make="Honda", model="Civic"))
        row = out["00000001-0000-0000-0000-000000000000"]
        assert "no asking price" in row["why_not"]

    def test_a_car_that_has_left_says_so(self):
        out = self._out(car(1, status="gone", make="Honda", model="Civic"))
        assert "left the market" in out[
            "00000001-0000-0000-0000-000000000000"]["why_not"]

    def test_a_car_a_rule_hides_says_so(self):
        out = self._out(car(1, filtered=True, make="Honda", model="Civic"))
        assert "rule of yours" in out[
            "00000001-0000-0000-0000-000000000000"]["why_not"]

    def test_a_car_with_no_year_says_so(self):
        out = self._out(car(1, year=None, make="Honda", model="Civic"))
        assert "model year" in out[
            "00000001-0000-0000-0000-000000000000"]["why_not"]

    def test_a_car_with_no_model_says_so(self):
        out = self._out(car(1))
        assert "make and model" in out[
            "00000001-0000-0000-0000-000000000000"]["why_not"]

    def test_every_car_in_a_mixed_list_carries_a_row(self):
        cars = [car(1, make="Honda", model="Civic"),
                car(2, make="Honda", model="Civic", price=None),
                car(3, make="Honda", model="Civic", status="gone"),
                car(4, make="Honda", model="Civic", filtered=True),
                car(5, year=None, make="Honda", model="Civic"),
                car(6)]
        out = insight.comparables(cars)
        assert len(out) == len(cars)
        for row in out.values():
            assert row.get("why_not") or row.get("pct") is not None \
                or row.get("rank") is not None

    def test_a_generator_is_read_once_and_still_answers_everything(self):
        cars = [car(i, make="Honda", model="Civic") for i in range(4)]
        assert len(insight.comparables(c for c in cars)) == len(cars)
