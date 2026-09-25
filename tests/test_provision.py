"""A fresh install must work without anybody configuring it first."""
import json
import re



from autotrader.config import Config
from autotrader.provision import (MOVED_NOTICE, bootstrap, ensure_dashboard_url,
                                  ensure_notifications, generate_topic,
                                  rotate_ntfy_topic, subscribe_url)

URL = "https://www.autotrader.ca/cars/honda/civic/?rcp=15&prx=-2"


class TestTopicGeneration:
    def test_topics_are_unguessable_and_unique(self):
        topics = {generate_topic() for _ in range(200)}
        assert len(topics) == 200
        assert all(len(t) >= 28 for t in topics)

    def test_topics_avoid_ambiguous_characters(self):
        """This gets typed into a phone by hand."""
        for _ in range(50):
            tail = generate_topic().split("-", 1)[1]
            assert not set(tail) & set("ilo01")

    def test_topics_are_url_safe(self):
        for _ in range(50):
            assert re.fullmatch(r"[a-z0-9-]+", generate_topic())


class TestFirstRunNotifications:
    def test_a_bare_install_configures_ntfy_by_itself(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        assert cfg.active_channels({}) == []

        result = ensure_notifications(cfg, {})
        assert result["changed"]
        assert cfg.active_channels({}) == ["ntfy"]
        assert subscribe_url(cfg).startswith("https://ntfy.sh/autotrader-")

    def test_nothing_is_touched_when_a_channel_already_works(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
        result = ensure_notifications(cfg, env)
        assert not result["changed"]
        assert not cfg.get("notifications.channels.ntfy.topic")

    def test_an_existing_topic_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("notifications.channels.ntfy.topic", "my-own-topic-abcdefgh")
        ensure_notifications(cfg, {})
        assert cfg.get("notifications.channels.ntfy.topic") == "my-own-topic-abcdefgh"

    def test_running_twice_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        ensure_notifications(cfg, {})
        topic = cfg.get("notifications.channels.ntfy.topic")
        assert not ensure_notifications(cfg, {})["changed"]
        assert cfg.get("notifications.channels.ntfy.topic") == topic


class TestMovingTheTopic:
    def test_the_topic_changes(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("notifications.channels.ntfy.topic", "old-public-topic")
        assert rotate_ntfy_topic(cfg)
        new = cfg.get("notifications.channels.ntfy.topic")
        assert new != "old-public-topic" and new.startswith("autotrader-")

    def test_the_notice_for_the_old_topic_says_nothing_of_where(self):
        assert "autotrader-" not in MOVED_NOTICE and "ntfy.sh/" not in MOVED_NOTICE

    def test_no_topic_means_nothing_to_move(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        assert not rotate_ntfy_topic(cfg)


class TestTheDashboardAddress:
    def test_it_is_filled_in_from_the_repository(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        ensure_dashboard_url(cfg, {"GITHUB_REPOSITORY": "someone/watch"})
        assert cfg.get("notifications.dashboard_url") == "https://someone.github.io/watch"

    def test_a_renamed_repository_is_followed(self, tmp_path):
        """Pages does not redirect a renamed repository's old address."""
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("notifications.dashboard_url", "https://someone.github.io/old-name")
        assert ensure_dashboard_url(cfg, {"GITHUB_REPOSITORY": "someone/watch"})["changed"]
        assert cfg.get("notifications.dashboard_url") == "https://someone.github.io/watch"

    def test_a_custom_address_is_left_alone(self, tmp_path):
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.set("notifications.dashboard_url", "https://cars.example.com")
        assert not ensure_dashboard_url(cfg, {"GITHUB_REPOSITORY": "someone/watch"})["changed"]
        assert cfg.get("notifications.dashboard_url") == "https://cars.example.com"


class TestBootstrap:
    def test_a_fresh_install_ends_up_usable(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        result = bootstrap(cfg, {})
        assert result["changed"]
        assert result["channels"] == ["ntfy"]
        assert result["subscribe_url"]
        # ...and it was persisted, not just held in memory.
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["notifications"]["channels"]["ntfy"]["topic"]

    def test_it_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        bootstrap(cfg, {})
        before = (tmp_path / "config.json").read_text()
        again = bootstrap(Config.load(tmp_path / "config.json"), {})
        assert not again["changed"]
        assert (tmp_path / "config.json").read_text() == before

    def test_it_defers_to_a_real_channel(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        result = bootstrap(cfg, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"})
        assert result["channels"] == ["telegram"]


class TestEverySetupStepCanBePrinted:
    """Setup runs before everything, so a crash in it is the bot down.

    It was. A step added later reported itself with "detail" where its
    siblings use "reason", the printer indexed with [] rather than .get, and
    every scheduled check failed on a KeyError for an hour before anybody
    looked at a log. Two things were wrong and both are worth a test: the
    step's shape, and the printer's tolerance of a shape it did not expect.
    """

    def test_every_step_carries_a_reason(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.save()
        result = bootstrap(cfg, {})

        assert result["steps"], "bootstrap reported no steps at all"
        for step in result["steps"]:
            assert "step" in step, step
            assert step.get("reason"), f"{step.get('step')} has no reason: {step}"
            assert isinstance(step["reason"], str) and step["reason"].strip()

    def test_a_step_in_an_unexpected_shape_does_not_kill_the_run(
            self, tmp_path, monkeypatch, capsys):
        """The printer is the last place that should be able to stop a check."""
        from autotrader import cli, provision as prov

        monkeypatch.chdir(tmp_path)
        cfg = Config.defaults(tmp_path / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?rcp=25", "Civic")
        cfg.save()
        monkeypatch.setattr(prov, "bootstrap", lambda *a, **k: {
            "changed": False,
            "steps": [{"step": "odd"}],           # no reason, no detail
            "subscribe_url": "", "channels": [],
        })
        # Built by the real parser rather than by hand, so this keeps working
        # when `setup` grows another flag - and proves the command runs with
        # exactly the arguments the CLI gives it.
        args = cli.build_parser().parse_args(
            ["--config", str(tmp_path / "config.json"),
             "--state", str(tmp_path / "state.json"),
             "setup", "--non-interactive"])
        assert cli.cmd_setup(args) == 0
        assert "odd" in capsys.readouterr().out
