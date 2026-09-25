import json
import os
import time

from autotrader.archive import archive_listing, prune, size_report
from autotrader.listing import Listing


def car():
    return Listing(id="123456", url="https://www.autotrader.ca/a/x/19_123456_/",
                   title="2021 Honda Civic", price=29999, mileage_km=52000,
                   images=["https://cdn.example/a.jpg"])


def test_archiving_off_writes_nothing(tmp_path):
    assert archive_listing(car(), {"mode": "off"}, root=tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_metadata_mode_writes_facts_but_not_the_page(tmp_path):
    folder = archive_listing(car(), {"mode": "metadata"}, root=tmp_path)
    data = json.loads((folder / "metadata.json").read_text())
    assert data["price"] == 29999 and data["archived_at"]
    assert not (folder / "page.html").exists()


def test_full_mode_stores_the_page(tmp_path):
    folder = archive_listing(car(), {"mode": "full"}, html="<html>hi</html>", root=tmp_path)
    assert (folder / "page.html").read_text() == "<html>hi</html>"


def test_images_are_only_downloaded_when_asked(tmp_path):
    class Fetcher:
        def __init__(self): self.calls = 0
        def get_bytes(self, url, referer=None): self.calls += 1; return b"\xff\xd8jpeg"

    f = Fetcher()
    archive_listing(car(), {"mode": "metadata", "images": 0}, fetcher=f, root=tmp_path)
    assert f.calls == 0
    folder = archive_listing(car(), {"mode": "metadata", "images": 1}, fetcher=f, root=tmp_path)
    assert f.calls == 1 and (folder / "photo_1.jpg").exists()


def test_archiving_never_raises(tmp_path):
    class Broken:
        def get_bytes(self, *a, **k): raise RuntimeError("nope")
    assert archive_listing(car(), {"mode": "metadata", "images": 3},
                           fetcher=Broken(), root=tmp_path) is not None


def test_retention_keeps_only_the_newest(tmp_path):
    for i, stamp in enumerate(["2020-01-01T00:00:00+00:00", "2024-01-01T00:00:00+00:00",
                               "2026-01-01T00:00:00+00:00"]):
        folder = tmp_path / f"listing{i}"
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"archived_at": stamp}))
    removed = prune({"keep_last": 2, "keep_days": 0}, root=tmp_path)
    assert len(removed) == 1 and removed == ["listing0"]


def test_dry_run_deletes_nothing(tmp_path):
    folder = tmp_path / "old"; folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({"archived_at": "2000-01-01T00:00:00+00:00"}))
    assert prune({"keep_days": 30, "keep_last": 0}, root=tmp_path, dry_run=True) == ["old"]
    assert folder.exists()


def test_a_folder_with_no_stamp_is_dated_by_when_it_was_written(tmp_path):
    folder = tmp_path / "old"; folder.mkdir()
    (tmp_path / "new").mkdir()
    long_ago = time.time() - 3 * 365 * 86400
    os.utime(folder, (long_ago, long_ago))
    assert prune({"keep_days": 30, "keep_last": 0}, root=tmp_path) == ["old"]
    assert (tmp_path / "new").exists()


def test_size_report_separates_pages_from_photos(tmp_path):
    folder = tmp_path / "x"; folder.mkdir()
    (folder / "page.html").write_text("x" * 1000)
    (folder / "photo_1.jpg").write_bytes(b"y" * 500)
    report = size_report(tmp_path)
    assert report["folders"] == 1
    assert report["html_bytes"] == 1000 and report["image_bytes"] == 500
