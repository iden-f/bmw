"""The change signals, and the argument for which ones exist.

Six candidates were on the table. Three are here and three are not, and the
reason in each case is what the data can actually support rather than what
sounds useful:

  BUILT   a car crossing back inside your rules. The price ceiling alone
          hides 114 of the 192 cars on this market. When one of them crosses
          the line that is the only moment its existence is ever mentioned -
          it is not new, and any price drop that caused it happened while the
          car was hidden and was never announced. Nothing generated an event
          for this at all; the runner fabricated a "new listing", which
          describes a car the reader may have already scrolled past.

  BUILT   relisted at a different number. RELISTED carried only the new price,
          so "back on the market" read the same whether the seller returned
          $4,000 cheaper or unchanged. The first is a motivated seller. The
          second is an expired listing.

  BUILT   the first photos on a listing that had none. 21 of 251 cars carry no
          photo at all, and a car you cannot see is a car you cannot judge.
          Recorded and shown, deliberately not pushed: nothing about the car
          changed, only what can be seen of it.

  NOT     description edited. No description is stored for any of the 251
          cars, and storing one costs a detail fetch per car per run against
          a 250-request budget. A signal that expensive has to be worth more
          than "the seller fixed a typo".

  LATER   seller type changed. This was "not built", on the grounds that
          seller_type was the empty string on all 251 listings - the parser
          extracted nothing for it, and alerting on a field that can never be
          set is worse than no feature because it looks like one. That turned
          out to be a bug rather than a limit: the answer was in the same JSON
          the parser was already reading. It is parsed now, and a change in it
          is recorded and shown. Still not pushed: a private seller consigning
          to a dealer is real news, and it is rare enough that the alert would
          arrive months apart and be forgotten in between.

  NOT     sitting unusually long. "Unusually" needs a baseline, and the watch
          is two days old with every still-listed figure right-censored. The
          market layer already refuses to state this for the same reason;
          alerting on it would be stating it anyway.
"""

from __future__ import annotations

from autotrader.listing import Listing
from autotrader.state import Change, State


def listing(price=90000, **over) -> Listing:
    data = {
        "id": "00000000-0000-4000-8000-000000000001",
        "title": "2021 Honda Civic Type R", "price": price,
        "url": "https://www.autotrader.ca/a/honda/civic/x/y/00000001/",
        "year": 2021, "make": "Honda", "model": "Civic",
        "price_source": "card", "images": [],
    }
    data.update(over)
    return Listing(**{k: v for k, v in data.items()
                      if k in Listing.__dataclass_fields__})


def fresh() -> State:
    return State({"version": 2, "listings": {}, "searches": {}, "runs": []})


class TestCrossingBackIntoYourRules:
    def test_a_hidden_car_that_qualifies_is_not_a_new_listing(self):
        st = fresh()
        st.record(listing(price=48000), filtered=True,
                  filter_reason="price $48,000 above maximum $40,000")
        change = st.record(listing(price=38000), filtered=False)
        assert change is not None
        # A price drop is the better headline when there is one - it carries
        # the numbers - so this case is the drop, not the crossing.
        assert change.kind == Change.PRICE_DROP
        assert st.listings[listing().id]["qualified_at"]

    def test_a_car_admitted_by_a_rule_change_rather_than_a_price_change(self):
        """You raised the ceiling. The car did nothing."""
        st = fresh()
        st.record(listing(price=42000), filtered=True,
                  filter_reason="price $42,000 above maximum $40,000")
        change = st.record(listing(price=42000), filtered=False)
        assert change is not None and change.kind == Change.QUALIFIED
        assert change.describe() == "Now within your rules"

    def test_it_remembers_which_rule_had_been_hiding_it(self):
        st = fresh()
        st.record(listing(price=42000), filtered=True,
                  filter_reason="price $42,000 above maximum $40,000")
        st.record(listing(price=42000), filtered=False)
        entry = st.listings[listing().id]
        assert "above maximum" in entry["qualified_from"]

    def test_a_car_that_was_never_hidden_does_not_get_the_event(self):
        st = fresh()
        st.record(listing(), filtered=False)
        assert st.record(listing(), filtered=False) is None

    def test_a_car_that_goes_from_hidden_to_hidden_does_not_either(self):
        """Its price still moved, and that is still recorded - the runner is
        what keeps quiet about a hidden car, not the ledger."""
        st = fresh()
        st.record(listing(price=48000), filtered=True, filter_reason="too dear")
        change = st.record(listing(price=45000), filtered=True,
                           filter_reason="too dear")
        assert change.kind == Change.PRICE_DROP
        assert "qualified_at" not in st.listings[listing().id]

    def test_a_car_coming_back_from_gone_is_relisted_not_qualified(self):
        """Two different stories; the reader should get the right one."""
        st = fresh()
        st.record(listing(), filtered=True, filter_reason="too dear")
        st.listings[listing().id]["status"] = "gone"
        change = st.record(listing(), filtered=False)
        assert change.kind == Change.RELISTED


class TestRelistedAtADifferentNumber:
    def test_back_cheaper_says_by_how_much(self):
        st = fresh()
        st.record(listing(price=90000))
        st.listings[listing().id]["status"] = "gone"
        change = st.record(listing(price=86000))
        assert change.kind == Change.RELISTED
        assert change.delta == -4000
        assert "back on the market $4,000 cheaper" in change.describe().lower()

    def test_back_dearer_says_so_too(self):
        st = fresh()
        st.record(listing(price=90000))
        st.listings[listing().id]["status"] = "gone"
        change = st.record(listing(price=94000))
        assert "dearer" in change.describe().lower()

    def test_back_at_the_same_number_is_just_back(self):
        st = fresh()
        st.record(listing(price=90000))
        st.listings[listing().id]["status"] = "gone"
        change = st.record(listing(price=90000))
        assert change.describe() == "Back on the market"

    def test_the_digest_line_carries_the_difference(self):
        from autotrader import render
        st = fresh()
        st.record(listing(price=90000))
        st.listings[listing().id]["status"] = "gone"
        change = st.record(listing(price=86000))
        assert "$4,000 cheaper" in render.headline([change])


class TestTheOnesNotBuiltAndWhy:
    """Guards on the reasons, so a later change has to face them.

    Each of these fails the day the underlying data changes, which is exactly
    when the decision deserves revisiting.
    """

    def test_the_seller_type_reason_has_since_been_answered(self):
        """This test used to assert the field was empty everywhere.

        It was: 251 listings, all of them the empty string, which was the
        whole reason for not building the signal. The answer turned out to be
        in the same JSON the parser was already reading, in both strategies
        that win on this site. So the field is real now, and the decision it
        justified has been revisited rather than left standing on a fact that
        stopped being true.

        The signal is recorded rather than pushed - see the module docstring.
        """
        from autotrader.parser import _seller_kind
        assert _seller_kind({"@type": "AutoDealer"}) == "dealer"
        assert _seller_kind({"seller": {"type": "Dealer"}}) == "dealer"

    def test_no_description_is_stored_to_compare_against(self):
        assert "description" not in Listing.__dataclass_fields__, (
            "a description is now stored, so 'we cannot see edits' is no "
            "longer the reason not to alert on them")


class TestPhotosArrivingOnACarThatHadNone:
    def test_it_is_recorded(self):
        st = fresh()
        st.record(listing(images=[]))
        st.record(listing(images=["https://cdn.test/1.webp"]))
        assert st.listings[listing().id]["photos_at"]

    def test_a_car_that_always_had_photos_does_not_get_it(self):
        st = fresh()
        st.record(listing(images=["https://cdn.test/1.webp"]))
        st.record(listing(images=["https://cdn.test/1.webp",
                                  "https://cdn.test/2.webp"]))
        assert "photos_at" not in st.listings[listing().id]

    def test_it_does_not_become_an_alert(self):
        """Nothing about the car changed, only what can be seen of it."""
        st = fresh()
        st.record(listing(images=[]))
        change = st.record(listing(images=["https://cdn.test/1.webp"]))
        assert change is None

    def test_it_does_reach_the_feed(self):
        from autotrader import insight
        st = fresh()
        st.record(listing(images=[]))
        st.record(listing(images=["https://cdn.test/1.webp"]))
        kinds = [e["kind"] for e in insight.events(st.listings.values())]
        assert "photos" in kinds


class TestTheFeedShowsTheNewKinds:
    def test_a_crossing_reaches_the_feed_with_the_rule_that_had_hidden_it(self):
        from autotrader import insight
        st = fresh()
        st.record(listing(price=42000), filtered=True,
                  filter_reason="price $42,000 above maximum $40,000")
        st.record(listing(price=42000), filtered=False)
        found = [e for e in insight.events(st.listings.values())
                 if e["kind"] == "qualified"]
        assert found and "above maximum" in found[0]["was_hidden_by"]

    def test_every_kind_the_feed_can_emit_has_a_label_on_the_page(self):
        """A kind with no entry in KIND renders as a blank chip."""
        import re
        from pathlib import Path
        js = Path("docs/app.js").read_text()
        block = re.search(r"const KIND = \{(.*?)\n\};", js, re.S).group(1)
        known = set(re.findall(r"^\s*(\w+):", block, re.M))
        emitted = set(re.findall(r'add\("(\w+)"', Path("autotrader/insight.py")
                                 .read_text()))
        assert emitted <= known, emitted - known


class TestADigestCannotBeLostToAMalformedChange:
    """describe() has to produce a sentence for anything it is handed.

    A TypeError inside digest rendering does not lose one alert, it loses
    every alert in the batch - and the whole point of holding undelivered
    alerts in state is that one is never lost.
    """

    @classmethod
    def every_kind(cls):
        return [v for k, v in vars(Change).items()
                if k.isupper() and isinstance(v, str)]

    def test_with_no_prices_at_all(self):
        for kind in self.every_kind():
            text = Change(kind, listing(price=None)).describe()
            assert text and isinstance(text, str), kind

    def test_with_only_one_of_the_two(self):
        for kind in self.every_kind():
            assert Change(kind, listing(), old_price=90000).describe()
            assert Change(kind, listing(), new_price=90000).describe()

    def test_the_digest_survives_one(self):
        from autotrader import render
        changes = [Change(Change.PRICE_DROP, listing(), old_price=90000,
                          new_price=86000),
                   Change(Change.PRICE_DROP, listing())]      # malformed
        assert render.headline(changes)
        assert render.as_text(changes)


class TestChangingHands:
    def test_private_to_dealer_is_recorded(self):
        st = fresh()
        st.record(listing(seller_type="private"))
        st.record(listing(seller_type="dealer"))
        entry = st.listings[listing().id]
        assert entry["seller_changed_at"] and entry["seller_was"] == "private"

    def test_it_is_not_an_alert(self):
        st = fresh()
        st.record(listing(seller_type="private"))
        assert st.record(listing(seller_type="dealer")) is None

    def test_an_unchanged_seller_records_nothing(self):
        st = fresh()
        st.record(listing(seller_type="dealer"))
        st.record(listing(seller_type="dealer"))
        assert "seller_changed_at" not in st.listings[listing().id]

    def test_a_field_arriving_for_the_first_time_is_not_a_change(self):
        """Every car in state predates the parser learning to read this."""
        st = fresh()
        st.record(listing(seller_type=""))
        st.record(listing(seller_type="dealer"))
        assert "seller_changed_at" not in st.listings[listing().id]

    def test_it_reaches_the_feed(self):
        from autotrader import insight
        st = fresh()
        st.record(listing(seller_type="private"))
        st.record(listing(seller_type="dealer"))
        found = [e for e in insight.events(st.listings.values())
                 if e["kind"] == "seller"]
        assert found and found[0]["was"] == "private"
