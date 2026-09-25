"""Changing what you watch, and what happens to the cars you watched before.

Removing a search has always retired its cars: they stop being live, they keep
their history, and the dashboard keeps showing them. That is the right default
for "I have stopped watching this one". It is the wrong one for "I am not
hunting that car any more" - a dashboard still full of one car long after its
searches were swapped for entirely different ones.
"""
from __future__ import annotations


import pytest

from autotrader import clock
from autotrader.cli import main
from autotrader.config import Config
from autotrader.listing import Listing
from autotrader.state import State


def car(lid: str, search_id: str, **kw) -> Listing:
    base = dict(id=lid, url=f"https://www.autotrader.ca/a/x/19_{lid}_/",
                title=f"Honda Civic {lid}", search_id=search_id,
                price=30000, price_source="detail")
    base.update(kw)
    return Listing(**base)


@pytest.fixture
def bench(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    kept = cfg.add_search("https://www.autotrader.ca/cars/toyota/corolla/?rcp=25", "keep")
    going = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "going")
    cfg.save()

    state = State(path=tmp_path / "state.json")
    state.record(car("keep-1", kept.id))
    for n in range(3):
        state.record(car(f"old-{n}", going.id))
    state.record_search_ok(kept.id, 1, "jsonld")
    state.record_search_ok(going.id, 3, "jsonld")
    state.save()
    return type("Bench", (), {"cfg": cfg, "path": tmp_path,
                              "keep": kept.id, "going": going.id,
                              "state": staticmethod(
                                  lambda: State.load(tmp_path / "state.json"))})


class TestForgetting:
    def test_cars_of_a_removed_search_are_dropped(self, bench):
        bench.cfg.remove_search(bench.going)
        bench.cfg.save()
        state = bench.state()
        dropped = state.forget_listings({bench.keep})
        assert len(dropped) == 3
        assert set(state.listings) == {"keep-1"}

    def test_the_cars_you_still_watch_are_untouched(self, bench):
        state = bench.state()
        state.forget_listings({bench.keep})
        assert state.listings["keep-1"]["status"] == "active"
        assert state.listings["keep-1"]["price_history"]

    def test_the_removed_search_stops_being_reported_as_broken(self, bench):
        state = bench.state()
        state.record_search_error(bench.going, "boom")
        state.forget_listings({bench.keep})
        assert bench.going not in (state.data.get("searches") or {})
        assert bench.keep in state.data["searches"]

    def test_a_car_still_owed_an_alert_is_never_dropped(self, bench):
        """Being told is the promise the ledger exists to keep. "You stopped
        watching" is not a reason to break it without saying so."""
        state = bench.state()
        state.listings["old-1"]["pending"] = True
        dropped = state.forget_listings({bench.keep})
        assert "old-1" not in dropped
        assert "old-1" in state.listings

    def test_a_car_with_no_search_belongs_to_nothing(self, bench):
        state = bench.state()
        state.listings["orphan"] = {"id": "orphan", "status": "gone",
                                    "notified": True, "price_history": []}
        state.forget_listings({bench.keep})
        assert "orphan" not in state.listings


class TestTheCommand:
    def test_without_yes_it_only_says_what_it_would_do(self, bench, capsys):
        bench.cfg.remove_search(bench.going)
        bench.cfg.save()
        before = (bench.path / "state.json").read_bytes()
        assert main(["forget"]) == 0
        assert "3 cars" in capsys.readouterr().out
        assert (bench.path / "state.json").read_bytes() == before

    def test_with_yes_it_writes(self, bench, capsys):
        bench.cfg.remove_search(bench.going)
        bench.cfg.save()
        assert main(["forget", "--yes"]) == 0
        assert "forgot 3 cars" in capsys.readouterr().out
        assert set(bench.state().listings) == {"keep-1"}

    def test_nothing_to_forget_says_so(self, bench, capsys):
        assert main(["forget", "--yes"]) == 0
        assert set(bench.state().listings) == {"keep-1", "old-0", "old-1", "old-2"}

    def test_remove_can_do_both_in_one_step(self, bench, capsys):
        assert main(["remove", bench.going, "--forget"]) == 0
        out = capsys.readouterr().out
        assert "removed" in out and "forgot 3 cars" in out
        assert set(bench.state().listings) == {"keep-1"}
        assert [s.id for s in Config.load(bench.path / "config.json").searches] == [bench.keep]

    def test_remove_on_its_own_says_the_cars_are_still_there(self, bench, capsys):
        assert main(["remove", bench.going]) == 0
        assert "3 cars from it are still in state" in capsys.readouterr().out
        assert len(bench.state().listings) == 4

    def test_counts_read_as_english(self, bench, capsys):
        """Terminal output in this project never says "1 car(s)"."""
        bench.cfg.remove_search(bench.going)
        bench.cfg.save()
        state = bench.state()
        del state.listings["old-1"], state.listings["old-2"]
        state.save()
        main(["forget", "--yes"])
        out = capsys.readouterr().out
        assert "forgot 1 car " in out and "(s)" not in out


class TestPruneActuallyCompacts:
    """The compaction inside prune() sat below its return statement.

    Nothing failed. Nothing looked wrong. State simply grew one price point
    per car per check, forever, while the test covering the compaction called
    insight.compact_history directly and passed the whole time.
    """

    def history(self, points: int) -> list[dict]:
        from datetime import timedelta
        start = clock.now() - timedelta(days=365)
        return [{"at": (start + timedelta(days=n)).isoformat(), "price": 90000 + n}
                for n in range(points)]

    def test_a_year_of_daily_prices_is_thinned(self, bench):
        state = bench.state()
        state.listings["keep-1"]["price_history"] = self.history(200)
        state.prune()
        kept = state.listings["keep-1"]["price_history"]
        assert 20 < len(kept) < 100, len(kept)

    def test_the_endpoints_survive(self, bench):
        state = bench.state()
        full = self.history(200)
        state.listings["keep-1"]["price_history"] = list(full)
        state.prune()
        kept = state.listings["keep-1"]["price_history"]
        assert kept[0] == full[0] and kept[-1] == full[-1]

    def test_a_short_history_is_left_alone(self, bench):
        state = bench.state()
        full = self.history(6)
        state.listings["keep-1"]["price_history"] = list(full)
        state.prune()
        assert state.listings["keep-1"]["price_history"] == full
