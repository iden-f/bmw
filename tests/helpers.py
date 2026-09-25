"""Test doubles shared by the runner and failure-mode suites."""

from pathlib import Path

from autotrader import notifiers as _notifiers
from autotrader.http import Response
from autotrader.notifiers import Notifier, Result

# Captured at import, before any test patches them. Re-reading
# notifiers.dispatch inside a test picks up whatever an outer fixture already
# substituted, and calling that recurses or silently ignores your channels.
ORIGINAL_DISPATCH = _notifiers.dispatch
ORIGINAL_ALERT = _notifiers.alert


def use_channels(monkeypatch, runner_module, channels, alert_channels=None):
    """Point the runner's notification calls at specific test doubles."""
    targets = alert_channels if alert_channels is not None else channels

    def alert(c, s, b, e=None, notifiers=None):
        # A send aimed at particular channels - the runner does this to reach
        # a topic it is about to stop using - is recorded rather than
        # performed. The doubles still receive the message, so nothing here
        # ever touches the real service, and the test can still see where the
        # runner meant to send it.
        if notifiers is not None:
            for channel in notifiers:
                where = str((channel.config or {}).get("topic") or channel.name)
                for double in targets:
                    if hasattr(double, "aimed_at"):
                        double.aimed_at.append(where)
                        double.aimed.append((where, s, b))
        return ORIGINAL_ALERT(c, s, b, e, targets)

    monkeypatch.setattr(
        runner_module.notifiers, "dispatch",
        lambda c, ch, r=None, e=None, n=None: ORIGINAL_DISPATCH(c, ch, r, e, channels))
    monkeypatch.setattr(runner_module.notifiers, "alert", alert)


class FakeFetcher:
    """Serves a search page, and a detail page for any listing we know."""

    def __init__(self, search_html, details=None, fail=None, budget=0):
        self.search_html = search_html
        self.details = details or {}
        self.fail = fail
        self.urls = []
        self.budget = budget
        self.spent = 0
        self.stats = {"requests": 0, "retries": 0, "failures": 0, "blocked": 0,
                      "budget": budget, "spent": 0}

    @property
    def budget_left(self):
        return max(0, self.budget - self.spent) if self.budget else 1_000_000

    def get(self, url, referer=None, allow_block=False):
        self.urls.append(url)
        self.spent += 1
        self.stats["requests"] = self.stats["spent"] = self.spent
        if self.fail:
            self.stats["failures"] += 1
            raise self.fail
        for listing_id, html in self.details.items():
            if f"_{listing_id}_" in url:
                return Response(url=url, status=200, text=html, elapsed_ms=1)
        return Response(url=url, status=200, text=self.search_html, elapsed_ms=1)

    def get_bytes(self, *a, **k):
        self.spent += 1
        return None

    def close(self):
        pass


class Capture(Notifier):
    """A notification channel that records instead of sending."""

    name = "capture"

    def __init__(self):
        super().__init__({}, {}, {})
        self.digests = []
        self.alerts = []
        # Where the runner aimed a send that named its own channels, rather
        # than letting the configured ones be picked.
        self.aimed_at = []
        self.aimed = []            # (where, subject, body) for each of those

    def _send(self, changes, run):
        self.digests.append(list(changes))
        return Result("capture", True)

    def _send_text(self, subject, body):
        self.alerts.append((subject, body))
        return Result("capture", True)

    def alerts_matching(self, phrase):
        """Alerts whose subject contains `phrase`, case-insensitively."""
        return [(s, b) for s, b in self.alerts if phrase.lower() in s.lower()]


# ----------------------------------------------------------- moving time

def next_check(minutes: float | None = None):
    """Move the clock on to when the next check would really happen.

    Two runs in the same second is not a thing this bot does. Tests that
    called run() twice in a row were asking the code to distinguish "the car
    is gone" from "the page rotated" using zero elapsed time, and several
    rules that are correct in production - the removal grace, the silence
    alarm, the deduplication floor - are undefined at that spacing. Driven
    from the same default the bot ships with, so a change to the schedule
    changes what the suite simulates.
    """
    from datetime import timedelta

    from autotrader import clock
    from autotrader.config import DEFAULTS
    if minutes is None:
        minutes = float(DEFAULTS["health"]["expected_interval_minutes"])
    if clock._FROZEN is None:
        clock.freeze(clock.now())
    return clock.advance(timedelta(minutes=minutes))


def a_check_later(minutes: float = 91):
    """Move the clock forward as two real checks would.

    A car is only "gone" after a stretch of real time has passed with nobody
    seeing it - not after a number of calls. Tests that used to call
    mark_missing twice in a row were asserting that two checks zero seconds
    apart proved a sale, which is the thing the rule exists to stop. They now
    say how long they waited, and the default is one minute past the dedupe
    floor: the closest together two scheduled checks are ever allowed to be.
    """
    from datetime import timedelta

    from autotrader import clock
    if clock._FROZEN is None:
        clock.freeze(clock.now())
    return clock.advance(timedelta(minutes=minutes))


# ------------------------------------------------------ configs a watch runs

#: Which config files the settings tests hold to their promises.
WATCH_CONFIGS = ("defaults", "installed", "example")

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.json"


def watch_config(where, tmp_path, monkeypatch):
    """A config as a watch would run it, built in ``tmp_path``.

    ``defaults`` is the in-memory default. ``installed`` is the config.json
    `setup` writes on a fresh install, and ``example`` is the file people
    copy; both are read back from disk, because an installed bot owns its own
    copy of every setting and changing a default changes nothing for it.

    Never the checkout's own config.json: that belongs to a live watch, lives
    encrypted, and is not in the repository to be read.
    """
    from autotrader.config import Config
    if where == "defaults":
        return Config.defaults()
    target = tmp_path / "config.json"
    if where == "example":
        target.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    elif where == "installed":
        from autotrader.cli import main
        monkeypatch.chdir(tmp_path)
        assert main(["--no-colour", "--config", str(target),
                     "--state", str(tmp_path / "state.json"),
                     "setup", "--non-interactive"]) == 0
        assert target.exists(), "setup wrote no config"
    else:
        raise ValueError(where)
    return Config.load(target)
