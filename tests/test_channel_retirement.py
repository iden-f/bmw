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
