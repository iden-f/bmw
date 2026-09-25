"""Our own copy of the photos, and everything a CDN can do to us.

The sandbox this was built in cannot reach autoscout24 at all, so every
screenshot taken during the rebuild showed the fallback and nobody had ever
seen a real card. That is the reason this module exists, and it is also the
reason it is tested this thoroughly rather than "verified" by looking: the
first time it runs against the real CDN is on a runner nobody is watching.

A photo may never fail a check. Every case below ends with the run intact.
"""

from __future__ import annotations

import json
import struct

import pytest

from autotrader import thumbs


def webp(width: int = 250, height: int = 188, pad: int = 900) -> bytes:
    """A minimal but genuinely parseable VP8X WebP."""
    body = (b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
            + b"\x00" * pad)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def png(width: int = 250, height: int = 188) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + struct.pack(">II", width, height) + b"\x00" * 400)


class Response:
    def __init__(self, content=b"", status_code=200, content_type="image/webp"):
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        self.text = ""


class CDN:
    """A stand-in that can misbehave in every way a real one does."""

    def __init__(self, **behaviour):
        self.behaviour = behaviour
        self.calls: list[str] = []

    def get(self, url, referer=None, allow_block=False):
        self.calls.append(url)
        act = self.behaviour.get("mode", "ok")
        if act == "raise":
            raise ConnectionError("connection reset by peer")
        if act == "404":
            return Response(b"not found", 404, "text/plain")
        if act == "html":
            return Response(b"<html>login</html>", 200, "text/html")
        if act == "huge":
            return Response(webp(pad=200_000), 200, "image/webp")
        if act == "lying":
            return Response(b"not an image at all", 200, "image/webp")
        return Response(webp(), 200, "image/webp")


def car(i: int, *, filtered=False, status="active", images=True) -> dict:
    return {
        "id": f"{i:08d}-0000-0000-0000-000000000000",
        "status": status, "filtered": filtered,
        "images": [f"https://cdn.test/{i}.webp"] if images else [],
    }


@pytest.fixture(autouse=True)
def here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(thumbs, "THUMB_DIR", tmp_path / "docs/thumbs")
    monkeypatch.setattr(thumbs, "INDEX", tmp_path / "docs/thumbs/index.json")
    return tmp_path


class TestReadingAnImageWithoutAnImageLibrary:
    """Dimensions come out of the file header. No Pillow in a 30-minute job."""

    def test_webp(self):
        assert thumbs._dimensions(webp(250, 188)) == (250, 188)

    def test_png(self):
        assert thumbs._dimensions(png(320, 240)) == (320, 240)

    def test_a_jpeg_header(self):
        """SOI carries no length. Treating the next two bytes as one walks
        the reader straight off the end of the file."""
        blob = (b"\xff\xd8"
                + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
                + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
                + struct.pack(">HH", 188, 250) + b"\x03" + b"\x00" * 9)
        assert thumbs._dimensions(blob) == (250, 188)

    def test_a_jpeg_with_restart_markers_before_the_frame(self):
        blob = (b"\xff\xd8" + b"\xff\xd0" + b"\xff\xd1"
                + b"\xff\xc2" + struct.pack(">H", 17) + b"\x08"
                + struct.pack(">HH", 600, 800) + b"\x03" + b"\x00" * 9)
        assert thumbs._dimensions(blob) == (800, 600)

    def test_something_that_is_not_an_image(self):
        assert thumbs._dimensions(b"<html>hello</html>") is None

    def test_a_truncated_file_does_not_explode(self):
        assert thumbs._dimensions(webp()[:9]) is None


class TestFetching:
    def test_a_good_photo_is_kept_and_measured(self, here):
        report = thumbs.sync([car(1)], CDN())
        assert report.fetched == 1 and report.failed == 0
        assert report.kept == 1
        saved = list((here / "docs/thumbs").glob("*.webp"))
        assert len(saved) == 1 and saved[0].stat().st_size > 0
        assert report.samples[0]["w"] == 250 and report.samples[0]["h"] == 188

    def test_the_index_records_where_it_went(self, here):
        thumbs.sync([car(1)], CDN())
        index = json.loads((here / "docs/thumbs/index.json").read_text())
        assert thumbs.local_for(car(1)["id"], index).startswith("thumbs/")

    def test_it_does_not_fetch_the_same_photo_twice(self, here):
        cdn = CDN()
        thumbs.sync([car(1)], cdn)
        thumbs.sync([car(1)], cdn)
        assert len(cdn.calls) == 1

    def test_hidden_and_gone_cars_are_not_worth_the_bytes(self, here):
        cdn = CDN()
        thumbs.sync([car(1, filtered=True), car(2, status="gone")], cdn)
        assert cdn.calls == []

    def test_a_car_with_no_photo_is_simply_skipped(self, here):
        cdn = CDN()
        assert thumbs.sync([car(1, images=False)], cdn).fetched == 0
        assert cdn.calls == []


class TestEverythingTheCdnCanDo:
    """Each of these ends with the run intact and the reason recorded."""

    @pytest.mark.parametrize("mode,why", [
        ("raise", "connection reset"),
        ("404", "HTTP 404"),
        ("html", "not an image"),
        ("lying", "does not parse"),
        ("huge", "over the"),
    ])
    def test_it_fails_softly_and_says_why(self, here, mode, why):
        report = thumbs.sync([car(1)], CDN(mode=mode))
        assert report.failed == 1 and report.fetched == 0
        assert not list((here / "docs/thumbs").glob("*.webp"))
        if mode != "raise":
            assert any(why in str(s.get("error", "")) for s in report.samples), report.samples

    def test_a_dead_cdn_does_not_stop_the_next_run_trying(self, here):
        thumbs.sync([car(1)], CDN(mode="404"))
        report = thumbs.sync([car(1)], CDN())
        assert report.fetched == 1


class TestStayingSmall:
    def test_a_run_only_fetches_so_many(self, here):
        cdn = CDN()
        report = thumbs.sync([car(i) for i in range(40)], cdn, limit=5)
        assert report.fetched == 5 and len(cdn.calls) == 5
        assert report.skipped == 35

    def test_no_budget_means_no_photos(self, here):
        report = thumbs.sync([car(i) for i in range(10)], CDN(), budget=0)
        assert report.fetched == 0
        assert any("budget" in n for n in report.notes), report.notes

    def test_it_stops_before_it_can_overrun_and_says_so(self, here):
        """Headroom for one more of the largest allowed image, then stop.

        A photo's size is not known until it has been fetched, so a budget
        checked only against what is already on disk always lets one more
        through - and "one more" at the per-file cap is 60 KB of repository
        nobody asked for. Reserving that cap is the honest version: several
        small photos fit, and the total can never cross the line.
        """
        budget = thumbs.MAX_BYTES_EACH + 2500
        report = thumbs.sync([car(i) for i in range(20)], CDN(), budget=budget)
        assert 0 < report.fetched < 20
        assert report.total_bytes <= budget
        assert any("budget" in n for n in report.notes), report.notes

    def test_a_car_that_leaves_takes_its_photo_with_it(self, here):
        thumbs.sync([car(1), car(2)], CDN())
        assert len((here / "docs/thumbs").glob("*.webp") and
                   list((here / "docs/thumbs").glob("*.webp"))) == 2

        report = thumbs.sync([car(1)], CDN())
        assert report.pruned == 1
        assert len(list((here / "docs/thumbs").glob("*.webp"))) == 1
        assert report.kept == 1

    def test_pruning_frees_room_for_a_live_car(self, here):
        """The budget is for cars you are watching, not cars you watched."""
        thumbs.sync([car(i) for i in range(4)], CDN())
        before = thumbs.Report(total_bytes=0)
        report = thumbs.sync([car(9)], CDN())
        assert report.pruned == 4 and report.fetched == 1
        assert before is not None


class TestNotWritingWhereItShouldNot:
    def test_a_listing_id_that_is_a_path_is_refused(self, here):
        nasty = dict(car(1), id="../../etc/passwd")
        report = thumbs.sync([nasty], CDN())
        assert report.fetched == 0 and report.failed == 1
        assert not (here / "etc").exists()

    def test_a_dry_run_writes_nothing(self, here):
        report = thumbs.sync([car(1)], CDN(), dry_run=True)
        assert report.fetched == 1
        assert not (here / "docs/thumbs").exists() or \
               not list((here / "docs/thumbs").glob("*.webp"))


class TestAgainstTheFetcherWeActuallyHave:
    """The first live run fetched nothing, and the stub said it would work.

    The test double had `.content` and `.headers` because that is what
    `requests` returns. The bot's own Fetcher returns a dataclass with `text`
    and `status` and neither of those, so every photo read as "not an image"
    and the report was confidently wrong about why. A stub shaped like the
    convenient thing rather than the real thing is worse than no stub.
    """

    def test_it_uses_the_binary_path_when_the_fetcher_has_one(self, here):
        seen = {}

        class RealShaped:
            """What autotrader.http.Fetcher actually offers."""
            def get(self, url, referer=None, allow_block=False):
                raise AssertionError("a photo must not go through the text path")

            def get_asset(self, url, referer=None):
                seen["url"] = url
                return {"status": 200, "type": "image/webp", "content": webp()}

        report = thumbs.sync([car(1)], RealShaped())
        assert report.fetched == 1, report.samples
        assert seen["url"].endswith(".webp")

    def test_the_real_fetcher_has_the_method_this_relies_on(self):
        """A rename on the other side would break this silently otherwise."""
        from autotrader.http import Fetcher
        assert hasattr(Fetcher, "get_asset")

    def test_a_budget_exhausted_asset_is_explained_not_guessed_at(self, here):
        class Spent:
            def get_asset(self, url, referer=None):
                return {"error": "request budget spent", "status": None}

        report = thumbs.sync([car(1)], Spent())
        assert report.failed == 1
        assert "budget" in report.samples[0]["error"]


class TestAskingForTheRightSize:
    """The photo URL carries its own dimensions, and the bot took what it
    was given.

    A live check after the watched searches changed: 25 photos attempted, 25
    failed, every one "over the 60000 cap" - because the listing pages now
    hand out `.../<id>.jpg/1920x1080.jpg` and a 1920px JPEG is 90-200 KB. The
    cap was right. The request was wrong.
    """

    BIG = "https://prod.pictures.autoscout24.net/listing-images/a_b.jpg/1920x1080.jpg"

    def test_the_smallest_useful_variant_is_asked_for_first(self):
        assert thumbs.variants(self.BIG)[0].endswith("/480x360.jpg")

    def test_the_original_is_still_the_last_resort(self):
        assert thumbs.variants(self.BIG)[-1] == self.BIG

    def test_a_url_that_is_already_small_is_not_asked_for_twice(self):
        small = "https://cdn.test/a.jpg/480x360.jpg"
        assert small not in thumbs.variants(small)[:-1]
        assert thumbs.variants(small)[-1] == small

    def test_a_url_with_no_size_in_it_is_left_alone(self):
        plain = "https://cdn.test/photo.webp"
        assert thumbs.variants(plain) == [plain]

    def test_the_extension_is_preserved(self):
        webp_url = "https://cdn.test/a.webp/1920x1080.webp"
        assert all(v.endswith(".webp") for v in thumbs.variants(webp_url))

    def test_nothing_is_asked_for_when_there_is_no_url(self):
        assert thumbs.variants("") == []

    def test_a_big_original_no_longer_costs_the_photo(self, here):
        """End to end: the CDN serves the size in the path, as the real one does."""

        class Sizing:
            def __init__(self):
                self.calls = []

            def get(self, url, referer=None, allow_block=False):
                self.calls.append(url)
                big = "1920x1080" in url
                return Response(webp(pad=200_000 if big else 900), 200, "image/webp")

        cdn = Sizing()
        entry = dict(car(1))
        entry["images"] = [self.BIG]
        report = thumbs.sync([entry], cdn)
        assert report.fetched == 1 and report.failed == 0, report.notes
        assert cdn.calls == [self.BIG.replace("1920x1080", "480x360")], cdn.calls

    def test_it_falls_back_rather_than_giving_up(self, here):
        """A size the CDN does not serve costs a request, never the photo."""

        class Picky:
            def __init__(self):
                self.calls = []

            def get(self, url, referer=None, allow_block=False):
                self.calls.append(url)
                if "480x360" in url:
                    return Response(b"not found", 404, "text/plain")
                return Response(webp(), 200, "image/webp")

        cdn = Picky()
        entry = dict(car(1))
        entry["images"] = [self.BIG]
        report = thumbs.sync([entry], cdn)
        assert report.fetched == 1 and report.failed == 0
        assert len(cdn.calls) == 2 and "250x188" in cdn.calls[1]

    def test_a_photo_that_is_too_big_at_every_size_still_reports_why(self, here):
        cdn = CDN(mode="huge")
        entry = dict(car(1))
        entry["images"] = [self.BIG]
        report = thumbs.sync([entry], cdn)
        assert report.fetched == 0 and report.failed == 1
        assert "over the" in report.samples[-1]["error"]
        assert len(report.samples) == 1, "one car, one sample"



class TestKeepingMoreThanOne:
    """The detail sheet draws a gallery. One local photo followed by seven
    hotlinked from the seller's CDN is the exact thing this module exists to
    stop - and it was invisible for three weeks because the machine that
    renders the screenshots cannot reach that CDN, so every one of those
    slides was a grey box in every picture anyone looked at."""

    def car_with(self, n: int) -> dict:
        return {"id": "aaaaaaaa-0000-0000-0000-000000000000",
                "status": "active", "filtered": False,
                "images": [f"https://cdn.test/{i}.webp/1920x1080.webp"
                           for i in range(n)]}

    def test_three_are_kept_by_default(self, here):
        cdn = CDN()
        report = thumbs.sync([self.car_with(8)], cdn)
        assert report.fetched == 3, report.notes
        assert len(thumbs.locals_for(self.car_with(8)["id"])) == 3

    def test_a_car_with_one_photo_keeps_one(self, here):
        report = thumbs.sync([self.car_with(1)], CDN())
        assert report.fetched == 1 and report.failed == 0

    def test_the_first_file_keeps_the_bare_id(self, here):
        """Every path written before there was more than one still resolves."""
        thumbs.sync([self.car_with(3)], CDN())
        first = thumbs.local_for(self.car_with(3)["id"])
        assert first == f"thumbs/{self.car_with(3)['id']}.webp"

    def test_they_come_back_in_the_sellers_order(self, here):
        thumbs.sync([self.car_with(3)], CDN())
        kept = thumbs.locals_for(self.car_with(3)["id"])
        assert kept == sorted(kept, key=lambda p: (len(p), p))

    def test_a_second_run_fetches_nothing_new(self, here):
        cdn = CDN()
        thumbs.sync([self.car_with(3)], cdn)
        before = len(cdn.calls)
        again = thumbs.sync([self.car_with(3)], cdn)
        assert again.fetched == 0
        assert len(cdn.calls) == before, "re-fetched photos it already had"

    def test_a_delisted_car_takes_all_of_its_photos_with_it(self, here):
        thumbs.sync([self.car_with(3)], CDN())
        report = thumbs.sync([], CDN())
        assert report.pruned == 3
        assert thumbs.locals_for(self.car_with(3)["id"]) == []

    def test_the_per_run_limit_counts_photos_not_cars(self, here):
        cars = [dict(self.car_with(3),
                     id=f"{i:08d}-0000-0000-0000-000000000000") for i in range(5)]
        report = thumbs.sync(cars, CDN(), limit=4)
        assert report.fetched == 4 and report.skipped

    def test_one_bad_angle_does_not_cost_three_requests(self, here):
        """A car whose second photo 404s stops there rather than hammering."""

        class Flaky:
            def __init__(self): self.calls = []

            def get(self, url, referer=None, allow_block=False):
                self.calls.append(url)
                if "/1." in url:
                    return Response(b"gone", 404, "text/plain")
                return Response(webp(), 200, "image/webp")

        cdn = Flaky()
        report = thumbs.sync([self.car_with(3)], cdn)
        assert report.fetched == 1
        assert len(thumbs.locals_for(self.car_with(3)["id"])) == 1
        assert sum(1 for c in cdn.calls if "/2." in c) == 0, "kept going after a failure"

    def test_an_index_written_before_this_still_works(self, here):
        """A single-file row from the old format keeps resolving."""
        thumbs.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        (thumbs.THUMB_DIR / "old.webp").write_bytes(webp())
        thumbs.INDEX.write_text(json.dumps(
            {"older-format-car": {"file": "old.webp", "bytes": 900, "w": 250, "h": 188}}))
        assert thumbs.local_for("older-format-car") == "thumbs/old.webp"
        assert thumbs.locals_for("older-format-car") == ["thumbs/old.webp"]
