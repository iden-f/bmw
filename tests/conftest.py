import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "tests" / "fixtures"
# Ten real listing pages the previous bot captured, kept gzipped. They used to
# be read straight out of archives/, but archives/ is data the bot prunes -
# compacting it away took fifty tests with it. A test fixture belongs in the
# test suite, where nothing else gets to delete it.
LISTING_PAGES = FIXTURES / "listings"


@pytest.fixture
def fixture_html():
    def read(name: str) -> str:
        return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")
    return read


@pytest.fixture
def archive_html():
    """Real listing pages captured by the previous version of the bot."""
    def read(listing_id: str) -> str:
        return gzip.decompress(
            (LISTING_PAGES / f"{listing_id}.html.gz").read_bytes()
        ).decode("utf-8", "replace")
    return read


@pytest.fixture
def archive_ids():
    if not LISTING_PAGES.exists():
        return []
    return sorted(p.name.replace(".html.gz", "")
                  for p in LISTING_PAGES.glob("*.html.gz"))


# ---------------------------------------------------------------- run harness

import pytest as _pytest  # noqa: E402

from autotrader import runner as runner_mod  # noqa: E402
from autotrader.config import Config  # noqa: E402
from autotrader.runner import run  # noqa: E402
from autotrader.state import State  # noqa: E402

from .helpers import Capture, FakeFetcher, next_check, use_channels  # noqa: E402

SEARCH = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&srt=35&prx=-2&loc=K1P"


@_pytest.fixture
def bench(tmp_path, monkeypatch, fixture_html, archive_html):
    """A working directory with a config, a fake site and a captured channel."""
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.add_search(SEARCH, "Example search")
    cfg.set("scraping.delay_ms", 0)
    cfg.set("scraping.retries", 0)
    cfg.set("archive.mode", "off")
    cfg.save()

    sink = Capture()
    use_channels(monkeypatch, runner_mod, [sink])

    details = {i: archive_html(i) for i in ("13166607", "68819631", "13221555")}

    def go(search_html=None, fail=None, config=None, minutes_later=None):
        # A schedule-interval between runs. Every countdown the bot keeps -
        # the removal grace, the silence alarm, how long a search has been
        # unreadable - is counted in elapsed time, and two runs in the same
        # second is not a cadence it can ever have.
        next_check(minutes_later)
        return run(config or cfg, State.load(tmp_path / "state.json"),
                   fetcher=FakeFetcher(search_html or fixture_html("search_cards"),
                                       details, fail))

    return type("Bench", (), {"cfg": cfg, "sink": sink, "run": staticmethod(go),
                              "path": tmp_path, "cards": fixture_html("search_cards")})


# --- the suite must not write to the repository it is testing ---------------
#
# A test that forgets to chdir into tmp_path runs the real bot in the real
# working tree. That happened: monkeypatch.undo() in one test reverted the
# fixture's own chdir along with the patch it meant to remove, so the run
# after it provisioned a fresh ntfy topic and rewrote the config here. Nothing
# failed. It was one `git add -A` from redirecting live alerts to a topic
# nobody is subscribed to, and the only reason it was caught is that a rebase
# happened to conflict on the file.
#
# So the suite watches its own hands. These are the files and folders the bot
# writes; if running the tests changes any of them, the tests are not running
# where they think they are. Taken from the vault's own list, so a file the bot
# starts keeping is watched here without anyone remembering to add it - and
# whether a checkout has them at all does not matter: "absent" is a state too,
# and a test that creates one has changed it.
def _repo_paths() -> tuple[tuple[str, ...], tuple[str, ...]]:
    from autotrader import budget, vault
    files = (*vault.PRIVATE_FILES, *vault.RETIRED_FILES, budget.STOP_FILE,
             "state.corrupt.json")
    folders = (*vault.PRIVATE_DIRS, str(vault.VAULT_DIR), "site")
    return files, folders


def _fingerprint() -> dict[str, str]:
    import hashlib
    files, folders = _repo_paths()
    out = {}
    for name in files:
        path = ROOT / name
        try:
            out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            out[name] = "absent"
    for name in folders:
        path = ROOT / name
        if not path.is_dir():
            out[name] = "absent"
            continue
        digest = hashlib.sha256()
        for item in sorted(p for p in path.rglob("*") if p.is_file()):
            stat = item.stat()
            digest.update(f"{item.relative_to(path)}:{stat.st_size}:"
                          f"{stat.st_mtime_ns}\n".encode())
        out[name] = digest.hexdigest()
    return out


@pytest.fixture(scope="session", autouse=True)
def _repo_is_left_alone():
    before = _fingerprint()
    yield
    changed = [name for name, digest in _fingerprint().items()
               if before.get(name) != digest]
    assert not changed, (
        "the test suite wrote to the repository it is testing: "
        + ", ".join(changed)
        + ". A test is running outside tmp_path - look for a missing "
          "monkeypatch.chdir, or a monkeypatch.undo() that reverted one."
    )


@pytest.fixture(autouse=True)
def _no_frozen_clock_leaks():
    """A test that stops time puts it back.

    autouse because a leaked freeze is invisible: the next test passes or
    fails for a reason that has nothing to do with the code it is testing,
    and which test froze it is not in the failure.
    """
    from autotrader import clock
    yield
    clock.freeze(None)


# --- the suite may not reach the internet -----------------------------------
#
# Every page these tests read is a fixture captured from the real site. That
# is a property worth enforcing rather than trusting: a test that quietly
# fetches autotrader.ca passes on a good day, fails during an outage, and
# tells you nothing either way - and one that quietly posts to ntfy sends a
# real notification to a real phone.
#
# Loopback is allowed: the dashboard tests serve docs/ on 127.0.0.1 and drive
# a browser against it, which is the whole point of them.

@pytest.fixture(scope="session", autouse=True)
def _no_internet():
    import socket

    real = socket.socket.connect
    allowed = {"127.0.0.1", "::1", "localhost"}

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in allowed:
            raise AssertionError(
                f"a test tried to open a connection to {host!r}. Every page "
                f"this suite reads is a fixture in tests/fixtures/; if you "
                f"need a new one, capture it with "
                f"`python -m autotrader capture` rather than fetching it "
                f"while the tests run.")
        return real(self, address)

    socket.socket.connect = connect
    try:
        yield
    finally:
        socket.socket.connect = real
