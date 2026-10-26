"""The Mac-hosted watcher: archive, ingest, outbox, media and API.

Everything runs against a temporary MACWATCH_HOME and the same fixtures the
bot's own tests use, so nothing here touches a network or the real Mac.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta
from pathlib import Path

import pytest

from autotrader import clock
from autotrader.config import Config
from autotrader.listing import Listing

from macwatch import ingest, legacy, media, outbox, settings as S, sources
from macwatch.db import Database
from macwatch.service import Service, in_quiet_hours

from .helpers import FakeFetcher

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SEARCH = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&prx=-2&loc=K1P"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MACWATCH_HOME", str(tmp_path / "macwatch"))
    S.ensure_dirs()
    return tmp_path / "macwatch"


@pytest.fixture
def db(home):
    return Database(S.db_path())


@pytest.fixture
def settings(home):
    # No quiet window: a test that measures the next due time against the
    # wall clock must not depend on the hour it is run at.
    st = S.Settings(ntfy_topic="trial-topic", interval_minutes=10,
                    quiet_start="00:00", quiet_end="00:00")
    st.ensure_token()
    st.save()
    return st


@pytest.fixture
def cfg(tmp_path):
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(SEARCH, "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("notifications.price_drop_min_abs", 250)
    cfg.set("notifications.price_drop_min_pct", 1.0)
    return cfg


@pytest.fixture
def pages():
    details = {i: gzip.decompress((FIXTURES / "listings" / f"{i}.html.gz").read_bytes())
               .decode("utf-8", "replace") for i in ("13166607", "68819631", "13221555")}
    return (FIXTURES / "search_cards.html").read_text(encoding="utf-8"), details


def car(lid="a1", price=30000, **kw) -> Listing:
    base = dict(id=lid, url=f"https://www.autotrader.ca/a/x/{lid}/", title="2019 Honda Civic",
                year=2019, make="Honda", model="Civic", price=price, price_source="search",
                mileage_km=50000, location="Ottawa", province="ON",
                images=[f"https://mkt-vehicleimages-prd.autotradercdn.ca/{lid}.jpg"])
    base.update(kw)
    return Listing(**base)


def take(db, settings, col, listings, site="autotrader", complete=True, confined=True):
    run = ingest.start_run(db, site, col.id, "test")
    summary = ingest.ingest(db, run, col, listings, complete=complete, confined=confined,
                            settings=settings)
    ingest.end_run(db, run, ok=True, listings=len(listings))
    return summary


def collection(db, cfg, **raw) -> ingest.Collection:
    if raw:
        cfg.data["searches"][0].update(raw)
    return ingest.sync_collections(db, cfg)[0]


# ------------------------------------------------------------------ schema

def test_migrations_are_idempotent(db):
    assert db.version() == 1
    assert Database(db.path).version() == 1
    assert db.counts()["listings"] == 0


def test_backup_is_a_consistent_copy(db, settings, cfg, home):
    col = collection(db, cfg)
    take(db, settings, col, [car()])
    copy = db.backup(home / "backups" / "copy.sqlite3")
    assert Database(copy).counts()["listings"] == 1


# ------------------------------------------------------------------ ingest

def test_first_read_is_a_baseline_and_announces_nothing(db, settings, cfg):
    col = collection(db, cfg)
    summary = take(db, settings, col, [car("a1"), car("a2")])
    assert summary.baseline and summary.fresh == 2 and summary.queued == 0
    reasons = {r["reason"] for r in db.q("SELECT reason FROM outbox")}
    assert reasons == {"baseline read: what was already for sale"}
    # The next new car is announced.
    summary = take(db, settings, col, [car("a1"), car("a2"), car("a3")])
    assert summary.queued == 1
    row = db.one("SELECT * FROM outbox WHERE status='pending'")
    assert row["listing_id"] == "a3" and row["kind"] == "new"
    payload = json.loads(row["payload_json"])
    assert payload["title"].startswith(S.TRIAL_LABEL)
    assert "Civic" in payload["title"]


def test_every_price_change_is_kept_beyond_forty(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car(price=50000)])
    for i in range(1, 61):
        take(db, settings, col, [car(price=50000 - i * 300)])
    points = db.q("SELECT price FROM price_events WHERE listing_id='a1' ORDER BY id")
    assert len(points) == 61
    assert points[-1]["price"] == 50000 - 60 * 300
    assert db.one("SELECT price FROM listings WHERE id='a1'")["price"] == 50000 - 60 * 300


def test_a_significant_drop_alerts_and_a_small_one_is_recorded_quietly(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car(price=30000)])
    small = take(db, settings, col, [car(price=29900)])
    assert small.queued == 0
    assert db.one("SELECT reason FROM outbox WHERE kind='price_drop'")["reason"] \
        == "small drop, below your threshold"
    big = take(db, settings, col, [car(price=28000)])
    assert big.queued == 1
    row = db.one("SELECT * FROM outbox WHERE status='pending'")
    assert row["kind"] == "price_drop"
    assert "down $1,900" in json.loads(row["payload_json"])["title"]
    assert len(db.q("SELECT 1 FROM price_events WHERE listing_id='a1'")) == 3


def test_card_and_detail_prices_are_not_compared_with_each_other(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car(price=30000, price_source="detail")])
    # The card says a few hundred less, as cards do: no event, no alert.
    take(db, settings, col, [car(price=29700, price_source="search")])
    assert len(db.q("SELECT 1 FROM price_events WHERE listing_id='a1'")) == 1
    assert db.one("SELECT price FROM listings WHERE id='a1'")["price"] == 30000
    assert not db.q("SELECT 1 FROM outbox WHERE kind LIKE 'price%'")
    # The card then drops against the last card: that is a real move.
    out = take(db, settings, col, [car(price=28000, price_source="search")])
    assert out.queued == 1


def test_silent_collection_never_sends(db, settings, cfg):
    col = collection(db, cfg, notify="archive")
    assert col.notify_mode == "archive"
    take(db, settings, col, [car(price=30000)])
    take(db, settings, col, [car(price=30000), car("b2", price=20000)])    # new car
    take(db, settings, col, [car(price=25000), car("b2", price=20000)])    # big drop
    take(db, settings, col, [car(price=25000)], complete=True)             # gone
    take(db, settings, col, [car(price=25000)], complete=True)
    take(db, settings, col, [car(price=25000), car("b2", price=20000)])    # back
    assert not db.q("SELECT 1 FROM outbox WHERE status='pending'")
    kinds = {r["kind"] for r in db.q("SELECT kind FROM outbox")}
    assert {"new", "price_drop", "removed", "relisted"} <= kinds
    assert all(r["reason"] in ("archive-only collection",
                               "baseline read: what was already for sale",
                               "removals are switched off", "relistings are switched off")
               for r in db.q("SELECT reason FROM outbox"))
    # And the history was recorded all the same.
    assert len(db.q("SELECT 1 FROM price_events WHERE listing_id='a1'")) == 2
    assert db.one("SELECT status FROM listings WHERE id='b2'")["status"] == "active"


def test_one_car_in_two_watches_is_stored_once_and_alerts_once(db, settings, cfg):
    cfg.add_search(SEARCH + "&x=1", "Second search")
    cfg.data["searches"][1]["notify"] = "archive"
    cols = ingest.sync_collections(db, cfg)
    for col in cols:
        take(db, settings, col, [car()])
    for col in cols:
        take(db, settings, col, [car(), car("n1")])
    assert db.one("SELECT COUNT(*) AS n FROM listings")["n"] == 2
    pending = db.q("SELECT * FROM outbox WHERE status='pending'")
    assert len(pending) == 1 and pending[0]["collection_id"] == cols[0].id


def test_mute_stops_alerts_but_not_history(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car(price=30000)])
    with db.tx() as conn:
        conn.execute("INSERT INTO marks(listing_id, muted, updated_at) VALUES('a1', 1, ?)",
                     (clock.stamp(),))
    out = take(db, settings, col, [car(price=20000)])
    assert out.queued == 0
    assert db.one("SELECT reason FROM outbox WHERE kind='price_drop'")["reason"] == "muted by you"
    assert len(db.q("SELECT 1 FROM price_events WHERE listing_id='a1'")) == 2


def test_absence_from_a_partial_read_is_not_evidence(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car("a1"), car("a2")])
    take(db, settings, col, [car("a1")], complete=False)
    take(db, settings, col, [car("a1")], complete=True, confined=False)
    assert db.one("SELECT status FROM listings WHERE id='a2'")["status"] == "active"
    take(db, settings, col, [car("a1")])
    assert db.one("SELECT status FROM listings WHERE id='a2'")["status"] == "not_seen_recently"
    take(db, settings, col, [car("a1")])
    row = db.one("SELECT status, gone_at FROM listings WHERE id='a2'")
    assert row["status"] == "unavailable" and row["gone_at"]
    # Gone is a status: the record, its prices and its membership remain.
    assert db.one("SELECT price FROM listings WHERE id='a2'")["price"] == 30000
    assert db.q("SELECT 1 FROM collection_listings WHERE listing_id='a2'")
    out = take(db, settings, col, [car("a1"), car("a2", price=28000)])
    assert db.one("SELECT status FROM listings WHERE id='a2'")["status"] == "active"
    assert any(c.kind == "relisted" for c in out.changes)


def test_a_car_outside_the_rules_is_kept_hidden_then_announced_when_it_qualifies(db, settings, cfg):
    cfg.set("filters.max_price", 25000)
    col = collection(db, cfg)
    take(db, settings, col, [car("z", price=20000)])
    out = take(db, settings, col, [car("z", price=20000), car("dear", price=40000)])
    assert out.hidden == 1 and out.queued == 0
    row = db.one("SELECT qualified, hidden_reason FROM listings WHERE id='dear'")
    assert row["qualified"] == 0 and row["hidden_reason"]
    out = take(db, settings, col, [car("z", price=20000), car("dear", price=24000)])
    assert out.queued == 1
    assert db.one("SELECT kind FROM outbox WHERE status='pending'")["kind"] == "qualified"


def test_pinned_listings_become_a_silent_collection(db, cfg):
    cfg.data["pinned"] = [{"id": "p1", "url": "https://www.autotrader.ca/a/honda/civic/x/19_12345678_/",
                           "name": "That Civic", "notify": "archive"},
                          "https://www.autotrader.ca/a/honda/accord/x/19_87654321_/"]
    cols = {c.id: c for c in ingest.sync_collections(db, cfg)}
    assert cols["pinned"].notify_mode == "archive"
    assert cols["pinned"].rules["urls"] == [
        "https://www.autotrader.ca/a/honda/civic/x/19_12345678_/",
        "https://www.autotrader.ca/a/honda/accord/x/19_87654321_/"]


def test_wrong_model_is_not_stored(db, settings, cfg):
    cfg.data["searches"][0]["filters"] = {"models": ["Civic"]}
    col = collection(db, cfg)
    out = take(db, settings, col, [car("c", model="Civic"), car("a", model="Accord")])
    assert out.wrong == 1 and db.one("SELECT COUNT(*) AS n FROM listings")["n"] == 1


def test_more_than_three_changes_go_out_as_one_digest(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car("seed")])
    out = take(db, settings, col, [car("seed")] + [car(f"n{i}") for i in range(5)])
    assert out.queued == 1
    row = db.one("SELECT * FROM outbox WHERE status='pending'")
    assert row["kind"] == "digest"
    payload = json.loads(row["payload_json"])
    assert len(payload["listing_ids"]) == 5 and "+4 new" in payload["title"]
    assert db.one("SELECT COUNT(*) AS n FROM outbox WHERE status='bundled'")["n"] == 5


def test_ingest_is_one_transaction(db, settings, cfg, monkeypatch):
    col = collection(db, cfg)
    take(db, settings, col, [car("a1")])
    broken = [car("a1"), car("boom")]
    monkeypatch.setattr(ingest, "_queue_media", lambda *a, **k: 1 / 0)
    run = ingest.start_run(db, "autotrader", col.id)
    with pytest.raises(ZeroDivisionError):
        ingest.ingest(db, run, col, broken, complete=True, confined=True, settings=settings)
    assert db.one("SELECT COUNT(*) AS n FROM listings")["n"] == 1
    assert db.one("SELECT COUNT(*) AS n FROM sightings")["n"] == 1


# ------------------------------------------------------------------ outbox

def test_outbox_retries_then_gives_up_and_keys_are_idempotent(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car("seed")])
    take(db, settings, col, [car("seed"), car("n1")])
    calls = []

    def flaky(payload):
        calls.append(payload)
        raise RuntimeError("HTTP 503: ntfy failed")

    assert outbox.deliver(db, settings, poster=flaky) == {"sent": 0, "failed": 0, "retry": 1, "held": 0}
    row = db.one("SELECT * FROM outbox WHERE kind='new' AND listing_id='n1'")
    assert row["status"] == "pending" and row["attempts"] == 1 and row["next_try_at"] > clock.stamp()
    # Not due yet: nothing sent again.
    assert outbox.deliver(db, settings, poster=flaky)["retry"] == 0
    clock.freeze(clock.now() + timedelta(minutes=5))
    try:
        sent = []
        out = outbox.deliver(db, settings, poster=lambda p: (sent.append(p), "msg-1")[1])
        assert out["sent"] == 1 and sent[0]["title"].startswith(S.TRIAL_LABEL)
        row = db.one("SELECT * FROM outbox WHERE kind='new' AND listing_id='n1'")
        assert row["status"] == "accepted" and row["accepted_at"] and row["error"] == "msg-1"
        # The same observation again writes no second row.
        take(db, settings, col, [car("seed"), car("n1")])
        assert db.one("SELECT COUNT(*) AS n FROM outbox WHERE listing_id='n1'")["n"] == 1
    finally:
        clock.freeze(None)


def test_a_rejected_request_is_not_retried(db, settings, cfg):
    col = collection(db, cfg)
    take(db, settings, col, [car("seed")])
    take(db, settings, col, [car("seed"), car("n1")])

    def refused(payload):
        raise RuntimeError("HTTP 400: ntfy rejected the request - bad topic")

    assert outbox.deliver(db, settings, poster=refused)["failed"] == 1
    assert db.one("SELECT status FROM outbox WHERE listing_id='n1'")["status"] == "failed"


def test_no_topic_means_recorded_not_sent(db, cfg, home):
    st = S.Settings(ntfy_topic="")
    col = collection(db, cfg)
    take(db, st, col, [car("seed")])
    out = take(db, st, col, [car("seed"), car("n1")])
    assert out.queued == 0
    assert db.one("SELECT reason FROM outbox WHERE listing_id='n1'")["reason"] == "no ntfy topic configured"


# ------------------------------------------------------------------- media

def test_photos_are_verified_deduplicated_and_kept(db, settings, cfg, home):
    col = collection(db, cfg)
    take(db, settings, col, [car("a1", images=[
        "https://mkt-vehicleimages-prd.autotradercdn.ca/1.jpg",
        "https://mkt-vehicleimages-prd.autotradercdn.ca/2.jpg",
        "https://evil.example.com/3.jpg",
        "https://mkt-vehicleimages-prd.autotradercdn.ca/bad.jpg"])])
    photo = (FIXTURES / "photo.webp").read_bytes()

    def fetch(url, referer):
        if url.endswith("bad.jpg"):
            return b"<html>not a photo</html>"
        return photo

    out = media.capture(db, settings, fetch=fetch, root=home / "media")
    assert out["saved"] == 2 and out["refused"] == 1 and out["failed"] == 1
    assets = db.q("SELECT * FROM media_assets")
    assert len(assets) == 1 and assets[0]["mime"] == "image/webp"
    assert (home / "media" / assets[0]["path"]).read_bytes() == photo
    states = {r["source_url"].rsplit("/", 1)[1]: r["status"] for r in db.q("SELECT * FROM listing_media")}
    assert states == {"1.jpg": "saved", "2.jpg": "saved", "3.jpg": "refused", "bad.jpg": "failed"}
    # A refused host stays refused until the allowlist changes; then it is tried.
    assert media.requeue_refused(db) == 0
    media.ALLOWED_HOST_SUFFIXES += ("evil.example.com",)
    try:
        assert media.requeue_refused(db) == 1
        assert media.capture(db, settings, fetch=fetch, root=home / "media")["saved"] == 1
    finally:
        media.ALLOWED_HOST_SUFFIXES = media.ALLOWED_HOST_SUFFIXES[:-1]
    # The listing goes; the photo stays.
    take(db, settings, col, [])
    take(db, settings, col, [])
    assert db.one("SELECT status FROM listings WHERE id='a1'")["status"] == "unavailable"
    assert (home / "media" / assets[0]["path"]).exists()
    assert media.photos_of(db, "a1")[0]["asset_id"] == assets[0]["id"]


def test_media_budget_pauses_capture_rather_than_pruning(db, settings, cfg, home):
    settings.media_budget_mb = 1
    col = collection(db, cfg)
    take(db, settings, col, [car("a1")])
    big = (home / "media" / "x.bin")
    big.write_bytes(b"\0" * 1_100_000)
    out = media.capture(db, settings, fetch=lambda u, r: b"", root=home / "media")
    assert out["paused"] == 1 and db.get("media_paused")["why"]
    assert db.one("SELECT status FROM listing_media")["status"] == "pending"


def test_sniff_and_dimensions():
    photo = (FIXTURES / "photo.webp").read_bytes()
    assert media.sniff(photo) == ("image/webp", ".webp")
    assert media.sniff(b"\x89PNG\r\n\x1a\n" + b"\0" * 20) == ("image/png", ".png")
    assert media.sniff(b"hello") is None
    assert media.allowed("https://scontent.xx.fbcdn.net/v/t.jpg")
    assert media.allowed("https://prod.pictures.autoscout24.net/listing-images/x.jpg")
    assert not media.allowed("http://mkt-vehicleimages-prd.autotradercdn.ca/x.jpg")
    assert not media.allowed("https://169.254.169.254/latest")


# ------------------------------------------------------------------ legacy

def test_legacy_import_keeps_history_and_is_idempotent(db, settings, cfg):
    col = collection(db, cfg)
    state = {"version": 2, "listings": {
        "13166607": {"id": "13166607", "title": "2021 BMW M5", "price": 98995, "year": 2021,
                     "make": "BMW", "model": "M5", "first_seen": "2026-09-01T00:00:00+00:00",
                     "last_seen": "2026-10-01T00:00:00+00:00", "status": "active",
                     "search_id": col.id, "shortlisted": True,
                     "price_history": [{"at": "2026-09-01T00:00:00+00:00", "price": 104000},
                                       {"at": "2026-09-20T00:00:00+00:00", "price": 98995}]},
        "fb-123456789": {"id": "fb-123456789", "title": "BMW M5", "price": 90000,
                         "first_seen": "2026-09-05T00:00:00+00:00", "status": "gone",
                         "last_seen": "2026-09-30T00:00:00+00:00", "price_history": []},
    }}
    counts = legacy.import_state(db, state, settings)
    assert counts == {"listings": 2, "price_events": 2, "memberships": 1, "marks": 1, "skipped": 0}
    assert legacy.import_state(db, state, settings)["skipped"] == 2
    assert db.one("SELECT site, status FROM listings WHERE id='fb-123456789'")["site"] == "marketplace"
    assert db.one("SELECT provenance FROM listings WHERE id='13166607'")["provenance"] == legacy.PROVENANCE
    # A known car arriving from a live read is not new, and its history continues.
    out = take(db, settings, col, [car("13166607", price=97000, title="2021 BMW M5",
                                       make="BMW", model="M5")])
    assert out.fresh == 0
    assert len(db.q("SELECT 1 FROM price_events WHERE listing_id='13166607'")) == 3


# ----------------------------------------------------------------- sources

def test_autotrader_pass_reads_fixtures_and_enriches_new_cars(db, settings, cfg, pages):
    html, details = pages
    report = sources.autotrader_pass(db, cfg, settings, fetcher=FakeFetcher(html, details))
    assert report.ok and report.runs[0]["fresh"] == 3
    row = db.one("SELECT * FROM listings WHERE id='13221555'")
    assert row["price_source"] == "detail" and row["mileage_km"]
    assert db.one("SELECT COUNT(*) AS n FROM listing_media")["n"] >= 3
    report = sources.autotrader_pass(db, cfg, settings, fetcher=FakeFetcher(html, details))
    assert report.runs[0]["fresh"] == 0
    runs = db.q("SELECT * FROM query_runs ORDER BY started_at")
    assert len(runs) == 2 and all(r["ok"] for r in runs) and runs[0]["strategy"]


def test_autotrader_pass_survives_a_failed_fetch(db, settings, cfg):
    from autotrader.http import FetchError
    report = sources.autotrader_pass(db, cfg, settings,
                                     fetcher=FakeFetcher("", fail=FetchError("HTTP 503")))
    assert not report.ok and "failed" in report.runs[0]["summary"]
    assert db.one("SELECT ok FROM query_runs")["ok"] == 0


class FakePage:
    def __init__(self, texts, session="ok"):
        self.texts, self.session, self.error, self.landed, self.status = texts, session, "", "", 200


class FakeBrowser:
    def __init__(self, texts, signed=True):
        self.texts, self.signed, self.visits = texts, signed, []

    def open(self):
        import contextlib
        return contextlib.nullcontext(self)

    def signed_in(self):
        return self.signed

    def visit(self, url, scrolls=0):
        self.visits.append(url)
        return FakePage(self.texts if "search" in url else [])

    def rest(self):
        pass


MARKETPLACE_DOC = json.dumps({"data": {"marketplace_search": {"feed_units": {"edges": [
    {"node": {"listing": {
        "__typename": "MarketplaceListing", "id": "123456789012",
        "marketplace_listing_title": "2018 Honda Civic Si",
        "listing_price": {"amount": "21000", "currency": "CAD"},
        "location": {"reverse_geocode": {"city": "Ottawa", "state": "ON"}},
        "custom_sub_titles_with_rendering_flags": [{"subtitle": "80K km"}],
        "primary_listing_photo": {"image": {"uri": "https://scontent.xx.fbcdn.net/x.jpg"}},
        "creation_time": 1760000000, "vehicle_make_display_name": "Honda",
        "vehicle_model_display_name": "Civic", "marketplace_listing_seller": {"__typename": "User"},
    }}}]}}}})


def test_marketplace_pass_through_a_fake_browser(db, settings, cfg):
    cfg.data["searches"][0]["filters"] = {"models": ["Civic"], "near": "Ottawa, ON"}
    browser = FakeBrowser([MARKETPLACE_DOC])
    report = sources.marketplace_pass(db, cfg, settings, browser_factory=lambda: browser)
    assert report.ok, report.line()
    assert any("facebook.com/marketplace" in v for v in browser.visits)
    row = db.one("SELECT * FROM listings WHERE site='marketplace'")
    assert row is not None and row["id"] == "fb-123456789012" and row["price"] == 21000
    assert row["mileage_km"] == 80000
    # Signed out: nothing read, said plainly.
    report = sources.marketplace_pass(db, cfg, settings,
                                      browser_factory=lambda: FakeBrowser([], signed=False))
    assert report.session == "signed_out" and not report.ok


# ----------------------------------------------------------------- service

def test_scheduler_runs_due_sources_once_and_persists_next_due(db, settings, cfg):
    calls = []

    def fake_pass(site):
        def run(db_, cfg_, st, trigger="schedule"):
            calls.append((site, trigger))
            return sources.PassReport(site)
        return run

    service = Service(settings, db, autotrader=fake_pass("autotrader"),
                      marketplace=fake_pass("marketplace"), config_provider=lambda: cfg)
    started = service.tick()
    assert sorted(started) == ["autotrader", "marketplace"]
    for site in started:
        service._running[site].join(5)
    assert sorted(calls) == [("autotrader", "schedule"), ("marketplace", "schedule")]
    assert service.tick() == []            # not due again
    nxt = service.next_due("autotrader")
    assert nxt is not None and 9 <= (nxt - clock.now()).total_seconds() / 60 <= 10.1
    assert Service(settings, db, config_provider=lambda: cfg).next_due("autotrader") == nxt
    job = service.request_check("autotrader")
    assert service.tick() == ["autotrader"]
    service._running["autotrader"].join(5)
    assert calls[-1] == ("autotrader", "check-now")
    assert service.job(job)["status"] == "succeeded"


def test_quiet_hours_slow_the_cadence(settings):
    settings.quiet_start, settings.quiet_end, settings.timezone = "02:00", "08:00", "America/Vancouver"
    from datetime import datetime, timezone
    night = datetime(2026, 10, 26, 11, 0, tzinfo=timezone.utc)    # 04:00 Pacific
    day = datetime(2026, 10, 26, 20, 0, tzinfo=timezone.utc)
    assert in_quiet_hours(settings, night) and not in_quiet_hours(settings, day)
    service = Service(settings, Database(":memory:"), config_provider=lambda: None)
    assert service.interval_minutes("autotrader", night) == 60
    assert service.interval_minutes("autotrader", day) == 10


def test_settings_validate_edits(settings):
    assert settings.apply({"interval_minutes": 15, "quiet_start": "01:30"}) == {}
    assert settings.interval_minutes == 15
    problems = settings.apply({"interval_minutes": 0, "quiet_end": "25:00", "timezone": "Mars/Olympus",
                               "api_token": "x", "app_url": "ftp://x"})
    assert set(problems) == {"interval_minutes", "quiet_end", "timezone", "api_token", "app_url"}
    assert settings.interval_minutes == 15
    assert S.Settings.load().interval_minutes == 10      # not saved yet
    settings.save()
    assert S.Settings.load().interval_minutes == 15
    assert settings.public()["api_token"] == "set"


# --------------------------------------------------------------------- API

@pytest.fixture
def client(db, settings, cfg, pages):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from macwatch.api import create_app
    html, details = pages
    sources.autotrader_pass(db, cfg, settings, fetcher=FakeFetcher(html, details))
    service = Service(settings, db, autotrader=lambda *a, **k: sources.PassReport("autotrader"),
                      marketplace=lambda *a, **k: sources.PassReport("marketplace"),
                      config_provider=lambda: cfg)
    return TestClient(create_app(db, settings, service)), settings


def test_api_refuses_without_the_token(client):
    c, st = client
    assert c.get("/api/listings").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/login?token=wrong").status_code == 401
    assert c.get("/api/listings", headers={"Authorization": f"Bearer {st.api_token}"}).status_code == 200


def test_api_lists_reads_edits_and_serves_the_app(client, home):
    c, st = client
    r = c.get(f"/login?token={st.api_token}", follow_redirects=False)
    assert r.status_code == 303 and "macwatch_token" in r.headers.get("set-cookie", "")
    assert "<title>Watch on Mac</title>" in c.get("/").text
    items = c.get("/api/listings?status=active").json()
    assert items["total"] == 3 and items["items"][0]["id"]
    lid = "13221555"
    detail = c.get(f"/api/listings/{lid}").json()
    assert detail["price_source"] == "detail" and detail["collections"][0]["name"] == "Example search"
    history = c.get(f"/api/listings/{lid}/history").json()
    assert history["prices"] and history["revisions"]
    assert c.get("/api/listings/nope").status_code == 404
    # Marks: same-origin writes only.
    r = c.patch(f"/api/listings/{lid}/marks", json={"shortlisted": True, "note": "call Monday"},
                headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.patch(f"/api/listings/{lid}/marks", json={"shortlisted": True, "note": "call Monday"})
    assert r.status_code == 200 and r.json()["shortlisted"] == 1
    assert c.get(f"/api/listings/{lid}").json()["note"] == "call Monday"
    assert c.patch(f"/api/listings/{lid}/marks", json={"shortlisted": "yes"}).status_code == 422
    # Settings round-trip with validation.
    r = c.patch("/api/settings", json={"interval_minutes": 7})
    assert r.status_code == 200 and S.Settings.load().interval_minutes == 7
    assert c.patch("/api/settings", json={"interval_minutes": 0}).status_code == 422
    assert c.get("/api/settings").json()["settings"]["api_token"] == "set"
    # Health and collections.
    health = c.get("/api/health").json()
    assert health["counts"]["listings"] == 3 and "autotrader" in health["sources"]
    cols = c.get("/api/collections").json()
    assert cols[0]["listings"] == 3 and cols[0]["notify_mode"] == "alert"
    # Check now returns a job the scheduler will pick up.
    job = c.post("/api/checks", json={"site": "autotrader"}).json()["job"]
    assert c.get(f"/api/jobs/{job}").json()["status"] == "queued"
    # Export carries the full history.
    export = c.get(f"/api/export/{cols[0]['id']}.json").json()
    assert len(export["listings"]) == 3 and export["listings"][0]["prices"]
    # Media: only a saved asset, by its id.
    assert c.get("/api/media/" + "a" * 64).status_code == 404
    assert c.get("/api/media/../../etc/passwd").status_code in (404, 422)
