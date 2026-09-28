"""A channel with dead credentials must switch itself off, not fail forever."""
import json

import pytest

from autotrader import runner as runner_mod
from autotrader.config import Config
from autotrader.notifiers import Notifier, Result, is_permanent_failure
from autotrader.state import State

from .helpers import use_channels

GMAIL_535 = ("(535, b'5.7.8 Username and Password not accepted. For more "
             "information, go to https://support.google.com/mail/?p=BadCredentials')")


class Dead(Notifier):
    """A channel whose credentials are rejected, the way Gmail rejects a
    revoked App Password."""
    name = "email"
    def _send(self, changes, run): raise RuntimeError(GMAIL_535)
    def _send_text(self, s, b): raise RuntimeError(GMAIL_535)


class Flaky(Notifier):
    name = "discord"
    def _send(self, changes, run): raise RuntimeError("HTTP 429: Too Many Requests")
    def _send_text(self, s, b): raise RuntimeError("HTTP 429: Too Many Requests")


class TestClassification:
    @pytest.mark.parametrize("detail", [
        GMAIL_535, "invalid_auth", "401 Unauthorized", "chat not found",
        "that webhook no longer exists - create a new one",
    ])
    def test_credential_errors_are_permanent(self, detail):
        assert is_permanent_failure(detail)

    @pytest.mark.parametrize("detail", [
        "HTTP 429: Too Many Requests", "HTTP 503", "Connection timed out",
        "HTTP 500", "temporary failure in name resolution",
    ])
    def test_transient_errors_are_not(self, detail):
        assert not is_permanent_failure(detail)

    def test_a_skipped_channel_is_never_permanent(self):
        assert not Result("x", False, GMAIL_535, skipped=True).permanent

    def test_a_success_is_never_permanent(self):
        assert not Result("x", True, "").permanent


class TestRetirement:
    def _wire(self, bench, monkeypatch, channels):
        use_channels(monkeypatch, runner_mod, channels)

    def _with_new_car(self, bench, listing_id="19_13999001_"):
        """A page with one more car on it, so the run has something to send.

        A card-only price change would not do: the bot disbelieves those until
        the listing page confirms them.
        """
        extra = f'''<div class="result-item">
    <a href="/a/honda/civic/ottawa/ontario/{listing_id}/">
      <h2>2023 Honda Civic Type R</h2>
      <span class="odometer-proximity">12,400 km</span>
      <span class="price-amount">$42,000</span>
    </a></div>'''
        return bench.cards.replace("</div></body></html>", extra + "</div></body></html>")

    def test_a_dead_channel_is_switched_off_after_two_runs(self, bench, monkeypatch):
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        first = bench.run()
        assert "email" not in first.disabled_channels, "one failure is not enough"

        second = bench.run(search_html=self._with_new_car(bench))
        assert "email" in second.disabled_channels

        saved = json.loads((bench.path / "config.json").read_text())
        assert saved["notifications"]["channels"]["email"]["enabled"] is False
        assert "disabled_reason" in saved["notifications"]["channels"]["email"]

    def test_it_stops_being_tried_afterwards(self, bench, monkeypatch):
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run(); bench.run(search_html=self._with_new_car(bench))
        # A real dispatch rebuilds channels from config, where it is now off.
        cfg = Config.load(bench.path / "config.json")
        assert "email" not in cfg.active_channels(
            {"GMAIL_USER": "u", "GMAIL_APP_PASSWORD": "p"})

    def test_a_run_that_sends_nothing_costs_no_strike(self, bench, monkeypatch):
        """No send means no failure and no log noise, so nothing to punish."""
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run()
        quiet = bench.run()              # same page, nothing to say
        assert quiet.channel_results == []
        assert quiet.disabled_channels == []

    def test_an_alert_counts_as_a_send(self, bench, monkeypatch, fixture_html):
        """Health and first-run alerts are where the noise actually comes from."""
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run(search_html=fixture_html("search_unreadable"))
        report = bench.run(search_html=fixture_html("search_unreadable"))
        assert "email" in report.disabled_channels

    def test_the_user_is_told_once_through_a_working_channel(self, bench, monkeypatch):
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run(); bench.run(search_html=self._with_new_car(bench))
        subjects = [s for s, _ in bench.sink.alerts]
        assert sum("Switched off email" in s for s in subjects) == 1
        body = next(b for s, b in bench.sink.alerts if "Switched off email" in s)
        assert "enabled" in body and "config.json" in body

    def test_the_way_back_it_gives_is_one_that_exists(self, bench, monkeypatch):
        """It sent the owner to a switch on the dashboard's Searches tab that
        the page has never had. config.json is sealed in the vault, so the
        way back is the vault's own round trip."""
        import shlex

        from autotrader.cli import build_parser
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run(); bench.run(search_html=self._with_new_car(bench))
        body = next(b for s, b in bench.sink.alerts if "Switched off email" in s)
        assert "dashboard" not in body and "Searches tab" not in body
        commands = [part.strip() for line in body.splitlines()
                    for part in line.split("&&") if "python -m autotrader" in part]
        assert any("set notifications.channels.email.enabled true" in c for c in commands)
        assert any("vault push --lease" in c for c in commands)
        for command in commands:
            words = shlex.split(command)[3:]
            build_parser().parse_args(words)       # exits if it is not a command

    def test_a_transient_failure_never_retires_a_channel(self, bench, monkeypatch):
        self._wire(bench, monkeypatch, [Flaky({}, {}, {}), bench.sink])
        for _ in range(5):
            report = bench.run()
        assert report.disabled_channels == []
        saved = json.loads((bench.path / "config.json").read_text())
        assert saved["notifications"]["channels"]["discord"]["enabled"] != False

    def test_a_recovery_resets_the_count(self, tmp_path):
        state = State(path=tmp_path / "s.json")
        assert state.record_channel("email", False, GMAIL_535, permanent=True) == 1
        assert state.record_channel("email", True) == 0
        assert state.record_channel("email", False, GMAIL_535, permanent=True) == 1

    def test_a_transient_failure_clears_the_permanent_count(self, tmp_path):
        """A wrong password followed by a timeout is not two strikes."""
        state = State(path=tmp_path / "s.json")
        state.record_channel("email", False, GMAIL_535, permanent=True)
        assert state.record_channel("email", False, "HTTP 503", permanent=False) == 0

    def test_the_failure_is_recorded_for_the_dashboard(self, bench, monkeypatch):
        self._wire(bench, monkeypatch, [Dead({}, {}, {}), bench.sink])
        bench.run()
        saved = json.loads((bench.path / "state.json").read_text())
        assert saved["channels"]["email"]["last_error"]
        assert saved["channels"]["email"]["permanent_failures"] == 1

    def test_going_silent_is_self_healing(self, bench, monkeypatch):
        """If the last channel retires, the next run provisions ntfy again."""
        self._wire(bench, monkeypatch, [Dead({}, {}, {})])
        bench.run(); bench.run(search_html=self._with_new_car(bench))
        cfg = Config.load(bench.path / "config.json")
        assert cfg.active_channels({}) == ["ntfy"]

    def test_a_retired_ntfy_is_not_set_up_again(self, bench, monkeypatch):
        """ntfy itself retired is not self-healing: setup switched it back on
        at the next firing, it failed twice and was retired again, for ever,
        warning "no channel was configured" each time round."""
        class DeadTopic(Dead):
            name = "ntfy"
        self._wire(bench, monkeypatch, [DeadTopic({}, {}, {})])
        bench.run(); bench.run(search_html=self._with_new_car(bench))
        assert not Config.load(bench.path / "config.json").active_channels({})

        later = bench.run(search_html=self._with_new_car(bench, "19_13999002_"))
        assert not any(w.startswith("setup:") for w in later.warnings), later.warnings
        ntfy = Config.load(bench.path / "config.json").get("notifications.channels.ntfy")
        assert ntfy["enabled"] is False and ntfy["disabled_reason"]


class TestASecretNeverReachesTheLog:
    """A connection error names the address it was sending to, and for
    Telegram and Discord that address is the secret. It went into the run
    log, the Status tab and, once the channel was switched off, into an
    alert on every other channel. The ids in it also held a "403", so an
    outage could pass for rejected credentials and switch the channel off.
    """

    TOKEN = "5401234567:AAFake403TokenValue"
    HOOK = "https://discord.com/api/webhooks/1140312345678901234/abcDEF403xyz"

    def _refused(self, monkeypatch, path):
        import requests

        def post(*a, **k):
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='example.invalid', port=443): Max "
                f"retries exceeded with url: {path} (Caused by "
                "NewConnectionError('Failed to establish a new connection'))")
        monkeypatch.setattr(requests, "post", post)

    def test_the_telegram_token_is_taken_out(self, monkeypatch):
        from autotrader.notifiers import TelegramNotifier
        self._refused(monkeypatch, f"/bot{self.TOKEN}/sendMessage")
        tg = TelegramNotifier({}, {"TELEGRAM_BOT_TOKEN": self.TOKEN,
                                   "TELEGRAM_CHAT_ID": "1"}, {})
        for result in (tg.send_text("subject", "body"), tg.verify()):
            assert not result.ok
            assert self.TOKEN not in result.detail
            assert "AAFake" not in result.detail
            assert not result.permanent, "an outage is not a rejected token"

    def test_the_discord_webhook_is_taken_out(self, monkeypatch):
        from autotrader.notifiers import DiscordNotifier
        self._refused(monkeypatch, "/api/webhooks/1140312345678901234/abcDEF403xyz")
        hook = DiscordNotifier({}, {"DISCORD_WEBHOOK_URL": self.HOOK}, {})
        result = hook.send_text("subject", "body")
        assert not result.ok
        assert "abcDEF403xyz" not in result.detail
        assert "1140312345678901234" not in result.detail
        assert not result.permanent

    def test_nor_does_it_reach_the_log(self, monkeypatch, caplog):
        import logging
        from autotrader.notifiers import TelegramNotifier
        self._refused(monkeypatch, f"/bot{self.TOKEN}/sendMessage")
        tg = TelegramNotifier({}, {"TELEGRAM_BOT_TOKEN": self.TOKEN,
                                   "TELEGRAM_CHAT_ID": "1"}, {})
        from autotrader.listing import Listing
        from autotrader.state import Change
        with caplog.at_level(logging.INFO):
            tg.send([Change(Change.NEW, Listing(id="1", title="Honda Civic"))])
        assert self.TOKEN not in caplog.text
        assert "failed" in caplog.text

    def test_a_real_rejection_still_reads_as_one(self, monkeypatch):
        import requests
        from autotrader.notifiers import TelegramNotifier

        class Refused:
            ok, status_code = False, 401
            def json(self):
                return {"ok": False, "description": "Unauthorized"}
        monkeypatch.setattr(requests, "post", lambda *a, **k: Refused())
        tg = TelegramNotifier({}, {"TELEGRAM_BOT_TOKEN": self.TOKEN,
                                   "TELEGRAM_CHAT_ID": "1"}, {})
        assert tg.send_text("subject", "body").permanent
