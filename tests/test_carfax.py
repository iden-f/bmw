"""The Carfax report a seller publishes: its link, and our copy of it.

The link lives in AutoTrader's page data and dies with the listing, so the
bot keeps the address and fetches the report itself. Only a report that is
already public is followed: a link to buy one is a shop, and is left alone.
A report may never fail a check, so every failure below ends with the run
intact.
"""

from __future__ import annotations

import json

import pytest

from autotrader import carfax
from autotrader.listing import Listing

VIEWER = "https://vhr.carfax.ca/?id=abcDEF123456xyz&ref=example"
SHOP = "https://www.carfax.ca/order?vin=XXXXXXXXXXXXXXXXX&report=verified&ref=example"


def page(url: str, mode: str = "view") -> str:
    """A listing page the way AutoTrader nests the report link in its data."""
    field = json.dumps({"carfax": {"carProofReportUrl": url, "carFaxMode": 1,
                                   "carProofMessage": f"history report - {mode}"}})
    return f"<html><script>window.__data = {field};</script><body>car</body></html>"


class TestFindingTheLink:

    def test_the_pages_own_field_is_read_with_its_escapes_undone(self):
        html = page(VIEWER).replace("/", "\\/").replace("&", "\\u0026")
        assert carfax.report_link(html) == VIEWER

    def test_a_link_to_buy_the_report_is_not_a_report(self):
        assert carfax.report_link(page(SHOP, "buy")) == ""
        assert not carfax.is_report_link(SHOP)

    def test_an_empty_field_means_no_report(self):
        assert carfax.report_link(page("", "request")) == ""
        assert carfax.report_link("") == ""
        assert carfax.report_link("<html>no data at all</html>") == ""

    def test_a_plain_anchor_is_the_fallback(self):
        html = f'<html><a class="x" href="{VIEWER}">History report</a></html>'
        assert carfax.report_link(html) == VIEWER

    def test_a_logo_or_the_front_page_is_not_a_report(self):
        for url in ("https://www.carfax.ca/", "https://www.carfax.ca/images/logo.png",
                    "https://example.com/?id=abcDEF123456xyz", "not a url"):
            assert not carfax.is_report_link(url), url

    def test_links_are_read_out_of_a_description(self):
        text = (f"Clean car, one owner. Carfax: {VIEWER}. Also see {SHOP} "
                "and call us. www.carfax.ca/order is not a report either.")
        assert carfax.links_in(text) == [VIEWER]

    def test_a_description_with_no_link_gives_nothing(self):
        assert carfax.links_in("Carfax available on request") == []
        assert carfax.links_in("") == []


class TestEnrichmentCarriesIt:

    def test_the_detail_page_puts_the_link_on_the_car(self):
        from autotrader.enrich import detail_from_html, enrich
        html = page(VIEWER) + (
            '<script type="application/ld+json">{"@type": "Car", "name": "2019 Example",'
            ' "offers": {"@type": "Offer", "price": "31000"}}</script>')
        detail = detail_from_html(html, "000000001")
        assert detail.carfax_url == VIEWER
        card = Listing(id="000000001", url="https://www.autotrader.ca/a/x/y/5_000000001_z/",
                       title="2019 Example", price=31000)
        enrich(card, html)
        assert card.carfax_url == VIEWER

    def test_a_captured_page_with_a_free_report_links_it(self, archive_html):
        from autotrader.enrich import detail_from_html
        detail = detail_from_html(archive_html("13221555"), "13221555")
        assert detail.carfax_url.startswith("https://vhr.carfax.ca/?id=")

    def test_a_captured_page_selling_the_report_links_nothing(self, archive_html):
        from autotrader.enrich import detail_from_html
        detail = detail_from_html(archive_html("13123827"), "13123827")
        assert detail.carfax_url == ""

    def test_a_marketplace_description_puts_the_link_on_the_record(self):
        from autotrader import marketplace as M
        node = {"id": "100000001", "marketplace_listing_title": "2019 Honda civic",
                "listing_price": {"amount": "20000.00"},
                "redacted_description": {"text": f"One owner. {VIEWER}"}}
        rec = M.record_of(node)
        assert rec["carfax_url"] == VIEWER
        search = type("S", (), {"id": "s1", "name": "Example search"})()
        car = M.to_listing(rec, search, make="Honda", models=["Civic"])
        assert car.carfax_url == VIEWER
        rec["carfax_url"] = SHOP
        assert M.to_listing(rec, search, make="Honda", models=["Civic"]).carfax_url == ""

    def test_the_collector_carries_the_link_from_the_cars_own_page(self):
        from collector.cycle import FACTS
        assert "carfax_url" in FACTS


# ---------------------------------------------------------------- the store

PDF = b"%PDF-1.4\n" + b"\x00" * 2000
HTML = b"<html><body>report viewer</body></html>"


class Response:
    def __init__(self, content=b"", status=200, kind="application/pdf"):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": kind}
        self.text = content.decode("utf-8", "replace") if kind.startswith("text") else ""


class Site:
    def __init__(self, mode="pdf"):
        self.mode = mode
        self.calls: list[str] = []

    def get(self, url, referer=None, allow_block=False):
        self.calls.append(url)
        if self.mode == "raise":
            raise ConnectionError("connection reset")
        if self.mode == "404":
            return Response(b"gone", 404, "text/plain")
        if self.mode == "html":
            return Response(HTML, 200, "text/html; charset=utf-8")
        if self.mode == "huge":
            return Response(b"%PDF-" + b"\x00" * (carfax.MAX_BYTES_EACH + 1))
        return Response(PDF)


def car(lid, url=VIEWER, status="active", first="2026-10-01T00:00:00Z", **more):
    return {"id": lid, "carfax_url": url, "status": status, "first_seen": first, **more}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "docs" / "reports"


class TestKeepingACopy:

    def test_a_pdf_is_saved_and_published(self, store):
        report = carfax.sync([car("000000001")], Site(), printer=None)
        assert report.fetched == 1 and report.kept == 1 and report.failed == 0
        assert (store / "000000001.pdf").read_bytes() == PDF
        held = carfax.local_for("000000001")
        assert held["file"] == "reports/000000001.pdf" and held["type"] == "pdf"
        assert held["url"] == VIEWER

    def test_a_report_served_as_a_page_is_printed_to_pdf(self, store):
        printed = []

        def printer(url):
            printed.append(url)
            return PDF

        report = carfax.sync([car("000000001")], Site("html"), printer=printer)
        assert printed == [VIEWER]
        assert report.fetched == 1 and report.samples[0]["printed"] is True
        assert (store / "000000001.pdf").read_bytes() == PDF

    def test_without_a_browser_the_page_itself_is_kept(self, store):
        report = carfax.sync([car("000000001")], Site("html"), printer=None)
        assert report.fetched == 1
        assert (store / "000000001.html").read_bytes() == HTML
        assert carfax.local_for("000000001")["type"] == "html"

    def test_a_printer_that_fails_falls_back_to_the_page(self, store):
        def printer(url):
            raise RuntimeError("no display")
        report = carfax.sync([car("000000001")], Site("html"), printer=printer)
        assert report.fetched == 1 and (store / "000000001.html").exists()

    def test_a_copy_already_held_is_not_fetched_again(self, store):
        site = Site()
        carfax.sync([car("000000001")], site, printer=None)
        carfax.sync([car("000000001")], site, printer=None)
        assert len(site.calls) == 1

    def test_a_new_link_replaces_the_old_copy(self, store):
        site = Site()
        carfax.sync([car("000000001")], site, printer=None)
        carfax.sync([car("000000001", url=VIEWER + "2")], Site("html"), printer=None)
        assert not (store / "000000001.pdf").exists()
        assert (store / "000000001.html").exists()

    @pytest.mark.parametrize("mode", ["raise", "404", "huge"])
    def test_every_failure_is_counted_and_the_run_goes_on(self, store, mode):
        report = carfax.sync([car("000000001")], Site(mode), printer=None)
        assert report.failed == 1 and report.kept == 0
        assert report.samples[0].get("error")
        assert carfax.local_for("000000001") is None

    def test_a_report_that_keeps_failing_is_left_alone_for_a_while(self, store):
        site = Site("404")
        for _ in range(4):
            carfax.sync([car("000000001")], site, printer=None)
        assert len(site.calls) == carfax.GIVE_UP_AFTER

    def test_the_run_fetches_only_so_many(self, store):
        site = Site()
        cars = [car(f"00000000{n}", first=f"2026-10-0{n}T00:00:00Z") for n in range(1, 8)]
        report = carfax.sync(cars, site, limit=2, printer=None)
        assert report.fetched == 2 and report.skipped == 5
        # The newest cars first: an alert just pointed at them.
        assert sorted(p.stem for p in store.glob("*.pdf")) == ["000000006", "000000007"]

    def test_a_full_store_says_so_and_keeps_what_it_has(self, store):
        report = carfax.sync([car("000000001"), car("000000002")], Site(),
                             budget=len(PDF) + carfax.MAX_BYTES_EACH - 1, printer=None)
        assert report.fetched == 1 and report.skipped == 1
        assert any("report store is full" in n for n in report.notes)

    def test_a_gone_car_keeps_its_copy_and_a_forgotten_one_loses_it(self, store):
        site = Site()
        carfax.sync([car("000000001"), car("000000002")], site, printer=None)
        report = carfax.sync([car("000000001", status="gone")], site, printer=None)
        assert (store / "000000001.pdf").exists()
        assert not (store / "000000002.pdf").exists()
        assert report.pruned == 1 and report.kept == 1

    def test_hidden_cars_and_cars_without_a_link_are_left_out(self, store):
        site = Site()
        carfax.sync([car("000000001", filtered=True), car("000000002", url="")], site,
                    printer=None)
        assert site.calls == []

    def test_a_dry_run_writes_nothing(self, store):
        site = Site()
        carfax.sync([car("000000001")], site, printer=None, dry_run=True)
        assert site.calls == [] and not store.exists()

    def test_the_settings_come_from_the_config(self):
        class Cfg:
            def __init__(self, **kv):
                self.kv = kv

            def get(self, key, default=None):
                return self.kv.get(key, default)

        assert carfax.settings(Cfg()) == {"budget": 300_000_000, "limit": 5}
        assert carfax.settings(Cfg(**{"dashboard.report_budget_mb": 50,
                                      "dashboard.reports_per_run": 2})) == \
            {"budget": 50_000_000, "limit": 2}
        assert carfax.settings(Cfg(**{"dashboard.reports_per_run": "lots"}))["limit"] == 5

    def test_printing_without_a_browser_gives_nothing(self, monkeypatch):
        monkeypatch.setattr(carfax, "chrome_binary", lambda: None)
        assert carfax.print_to_pdf(VIEWER) is None

    def test_printing_with_a_stand_in_browser(self, tmp_path, monkeypatch):
        script = tmp_path / "chrome"
        script.write_text("#!/bin/sh\nfor a in \"$@\"; do case \"$a\" in --print-to-pdf=*) "
                          "printf '%%PDF-1.4 fake' > \"${a#--print-to-pdf=}\";; esac; done\n")
        script.chmod(0o755)
        assert carfax.print_to_pdf(VIEWER, str(script)).startswith(b"%PDF-")


class TestItReachesThePage:

    def test_the_dashboard_publishes_the_link_and_the_copy(self, store):
        from autotrader.config import Config
        from autotrader.dashboard import build_payload
        from autotrader.state import State
        cfg = Config.defaults(store.parent.parent / "config.json")
        search = cfg.add_search("https://www.autotrader.ca/cars/honda/civic/reg_on/"
                                "cit_toronto/?zip=Toronto&zipr=500", "Civics")
        state = State(path=store.parent.parent / "state.json")
        state.record(Listing(id="000000001", url="https://www.autotrader.ca/offers/x-000000001",
                             title="2021 Honda Civic", year=2021, make="Honda", model="Civic",
                             price=30000, search_id=search.id, carfax_url=VIEWER))
        carfax.sync(state.listings.values(), Site(), printer=None)
        payload = build_payload(cfg, state, {})
        row = payload["listings"][0]
        assert row["carfax_url"] == VIEWER
        assert row["carfax"]["file"] == "reports/000000001.pdf"
        assert row["carfax"]["type"] == "pdf"

    def test_the_site_seals_the_copies_under_hashed_names(self, tmp_path):
        from autotrader import vault as V
        docs = tmp_path / "docs"
        (docs / "reports").mkdir(parents=True)
        (docs / "reports" / "000000001.pdf").write_bytes(PDF)
        (docs / "reports" / "index.json").write_text("{}")
        (docs / "index.html").write_text("<html></html>")
        vault = V.Vault.unlock(tmp_path, {V.ENV_KEY: "a passphrase long enough"}, create=True)
        out = tmp_path / "site"
        counts = vault.publish(docs, out)
        assert counts["reports"] == 1
        names = [p.name for p in (out / "reports").iterdir()]
        assert names == [f"{vault.report_name('000000001.pdf')}.bin"]
        blob = (out / "reports" / names[0]).read_bytes()
        assert V.unseal(vault.key, blob, f"reports/{names[0]}") == PDF
        assert V.unsafe_site_dir(tmp_path, out) == ""

    def test_the_vault_seals_the_store_with_the_rest(self):
        from autotrader import vault as V
        assert V.PRIVATE_DIRS["docs/reports"] == "reports"

    def test_the_page_knows_how_to_open_a_copy(self):
        from pathlib import Path
        js = Path("docs/app.js").read_text(encoding="utf-8")
        assert "function reportUrl" in js and "report/${file}" in js
        assert "History report" in js and "carfax_url" in js
