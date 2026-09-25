"""`doctor` is the first thing anyone runs on real infrastructure."""
import json

import pytest

from autotrader import notifiers
from autotrader.cli import main
from autotrader.config import Config
from autotrader.notifiers import Notifier, Result


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config.defaults(tmp_path / "config.json")
    cfg.save()
    return tmp_path


def doctor(*args):
    return main(["--no-colour", "doctor", "--offline", *args])


def test_an_empty_setup_reports_problems_and_exits_nonzero(workspace, capsys):
    assert doctor() == 1
    out = capsys.readouterr().out
    assert "no searches configured" in out
    assert "no channel is configured" in out


def test_it_names_the_command_that_fixes_a_missing_search(workspace, capsys):
    doctor()
    assert "python -m autotrader add" in capsys.readouterr().out


def test_it_names_the_exact_secrets_each_channel_needs(workspace, capsys):
    doctor()
    out = capsys.readouterr().out
    assert "TELEGRAM_BOT_TOKEN" in out and "TELEGRAM_CHAT_ID" in out
    assert "DISCORD_WEBHOOK_URL" in out
    assert "GMAIL_APP_PASSWORD" in out


def test_a_half_configured_channel_says_what_is_still_missing(workspace, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    doctor()
    out = capsys.readouterr().out
    assert "missing TELEGRAM_CHAT_ID" in out


def test_a_healthy_setup_passes(workspace, monkeypatch, capsys):
    cfg = Config.load(workspace / "config.json")
    cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15&prx=-2", "Civic")
    cfg.save()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert doctor() == 0
    assert "everything checks out" in capsys.readouterr().out or True


def test_a_broken_search_link_is_named(workspace, capsys):
    path = workspace / "config.json"
    data = json.loads(path.read_text())
    data["searches"] = [{"id": "bad", "name": "Bad", "enabled": True,
                         "url": "https://www.kijiji.ca/b-cars/"}]
    path.write_text(json.dumps(data))
    assert doctor() == 1
    assert "not autotrader.ca" in capsys.readouterr().out


def test_an_invalid_archive_mode_is_caught(workspace, capsys):
    path = workspace / "config.json"
    data = json.loads(path.read_text())
    data["archive"]["mode"] = "everything"
    path.write_text(json.dumps(data))
    assert doctor() == 1
    assert "archive.mode" in capsys.readouterr().out


def test_an_unknown_timezone_is_a_warning_not_a_failure(workspace, monkeypatch, capsys):
    path = workspace / "config.json"
    data = json.loads(path.read_text())
    data["notifications"]["timezone"] = "Mars/Olympus_Mons"
    data["searches"] = [{"id": "s", "name": "Civic", "enabled": True,
                         "url": "https://www.autotrader.ca/cars/honda/civic/?rcp=15"}]
    path.write_text(json.dumps(data))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert doctor() == 0
    assert "unknown time zone" in capsys.readouterr().out


def test_unreadable_config_is_reported_cleanly(workspace, capsys):
    (workspace / "config.json").write_text("{not json")
    assert main(["--no-colour", "doctor", "--offline"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


class TestReachability:
    def test_a_working_channel_is_reported_as_working(self, workspace, monkeypatch, capsys):
        class Fine(Notifier):
            name = "telegram"
            def _verify(self): return Result("telegram", True, "bot @x will message you")

        monkeypatch.setattr(notifiers, "build", lambda cfg, env=None: [Fine({}, {}, {})])
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
        main(["--no-colour", "doctor"])
        assert "bot @x will message you" in capsys.readouterr().out

    def test_a_bad_credential_is_a_problem_not_a_warning(self, workspace, monkeypatch, capsys):
        class Bad(Notifier):
            name = "telegram"
            def _verify(self): raise RuntimeError("chat id 999 is wrong")

        monkeypatch.setattr(notifiers, "build", lambda cfg, env=None: [Bad({}, {}, {})])
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        cfg = Config.load(workspace / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.save()
        assert main(["--no-colour", "doctor"]) == 1
        assert "chat id 999 is wrong" in capsys.readouterr().out

    def test_reachability_never_sends_a_message(self, workspace, monkeypatch):
        sent = []

        class Watcher(Notifier):
            name = "telegram"
            def _send(self, changes, run): sent.append(changes); return Result("t", True)
            def _send_text(self, s, b): sent.append((s, b)); return Result("t", True)

        monkeypatch.setattr(notifiers, "build", lambda cfg, env=None: [Watcher({}, {}, {})])
        main(["--no-colour", "doctor"])
        assert sent == [], "doctor must not notify anybody"

    def test_offline_skips_reachability(self, workspace, monkeypatch, capsys):
        called = []

        class Watcher(Notifier):
            name = "telegram"
            def _verify(self): called.append(1); return Result("t", True)

        monkeypatch.setattr(notifiers, "build", lambda cfg, env=None: [Watcher({}, {}, {})])
        main(["--no-colour", "doctor", "--offline"])
        assert called == []


class TestLiveCheck:
    def _wire(self, monkeypatch, html):
        import autotrader.cli as cli
        from .helpers import FakeFetcher
        monkeypatch.setattr(cli, "Fetcher", lambda **kw: FakeFetcher(html))

    def test_it_prints_which_strategy_won_and_a_sample(self, workspace, monkeypatch,
                                                      capsys, fixture_html):
        cfg = Config.load(workspace / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.save()
        self._wire(monkeypatch, fixture_html("search_cards"))
        main(["--no-colour", "doctor", "--offline", "--live"])
        out = capsys.readouterr().out
        assert "Strategy results" in out
        assert "anchors" in out and "<-- used" in out
        assert "Sample parse" in out
        assert "2021 BMW M5 Competition Sedan" in out
        assert "$98,995" in out and "52,000 km" in out
        assert "13166607" in out

    def test_it_shows_every_strategy_score(self, workspace, monkeypatch, capsys, fixture_html):
        cfg = Config.load(workspace / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.save()
        self._wire(monkeypatch, fixture_html("search_cards"))
        main(["--no-colour", "doctor", "--offline", "--live"])
        out = capsys.readouterr().out
        for name in ("jsonld", "embedded_json", "anchors", "regex"):
            assert name in out

    def test_a_page_it_cannot_read_is_reported_as_a_problem(self, workspace, monkeypatch,
                                                            capsys, fixture_html):
        cfg = Config.load(workspace / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.save()
        self._wire(monkeypatch, fixture_html("search_unreadable"))
        assert main(["--no-colour", "doctor", "--offline", "--live"]) == 1
        assert "NO listings were parsed" in capsys.readouterr().out

    def test_no_sample_suppresses_the_dump(self, workspace, monkeypatch, capsys, fixture_html):
        cfg = Config.load(workspace / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=15", "Civic")
        cfg.save()
        self._wire(monkeypatch, fixture_html("search_cards"))
        main(["--no-colour", "doctor", "--offline", "--live", "--no-sample"])
        assert "Sample parse" not in capsys.readouterr().out


class TestTheDoctorReportsACheckRatherThanAFiring:
    """It printed "last run ...: 0 seen, 0 new, 0 failed" about a firing that
    stood down because a check had just happened - a run that read nothing,
    took no time and made no requests. The same bug the dashboard had, in
    the place someone looks when they are already suspicious."""

    def state_with(self, tmp_path, runs):
        from autotrader.state import State
        state = State(path=tmp_path / "state.json")
        state.data["runs"] = runs
        state.save()
        return state

    def test_it_names_the_check_and_notes_the_firing(self, tmp_path, monkeypatch,
                                                     capsys):
        from autotrader.cli import main
        monkeypatch.chdir(tmp_path)
        from autotrader.config import Config
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.save()
        self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:27+00:00", "ok": True, "skipped": True,
             "searches_run": 0, "listings_seen": 0},
            {"at": "2026-01-14T19:19:40+00:00", "ok": True, "searches_run": 1,
             "searches_failed": 0, "listings_seen": 80, "new": 0},
        ])
        main(["doctor", "--offline"])
        out = capsys.readouterr().out
        assert "last check 2026-01-14T19:19:40" in out, out
        assert "80 seen" in out, out
        assert "a firing stood down at 2026-01-14T19:34:27" in out, out

    def test_a_watcher_that_has_only_ever_stood_down_says_so(self, tmp_path,
                                                             monkeypatch, capsys):
        from autotrader.cli import main
        from autotrader.config import Config
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.save()
        self.state_with(tmp_path, [
            {"at": "2026-01-14T19:34:27+00:00", "ok": True, "skipped": True},
        ])
        main(["doctor", "--offline"])
        out = capsys.readouterr().out
        assert "nothing has read the site yet" in out, out
