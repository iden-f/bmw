"""A whole check's worth of vault handling, against a real git remote.

What the workflows do, in order: pull the vault branch, open it, run, seal,
push, publish. The first run of a repository that used to keep its data in
the clear also has to move that data in, and move ntfy to a topic that was
never public.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

from autotrader import cli
from autotrader import vault as V
from autotrader.config import Config

PHRASE = "correct horse battery staple"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    git(work, "remote", "add", "origin", str(remote))
    for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
        git(work, "config", key, value)
    (work / "README.md").write_text("code\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "code")
    git(work, "push", "-q", "origin", "HEAD:main")
    monkeypatch.chdir(work)
    monkeypatch.setenv(V.ENV_KEY, PHRASE)
    return {"work": work, "remote": remote}


def vault_cli(*args):
    return cli.main(["vault", *args])


def test_pull_with_no_branch_yet_leaves_an_empty_vault(repo):
    assert vault_cli("pull") == 0
    assert (repo["work"] / "vault").is_dir()
    assert not any((repo["work"] / "vault").iterdir())


def test_a_run_round_trips_through_the_branch(repo, tmp_path):
    work = repo["work"]
    vault_cli("pull")
    assert vault_cli("open") == 0
    (work / "state.json").write_text('{"listings": {"x-1": {"title": "A car"}}}')
    (work / "docs" / "thumbs").mkdir(parents=True)
    (work / "docs" / "thumbs" / "x-1.webp").write_bytes(b"photo")
    assert vault_cli("seal") == 0
    assert vault_cli("push") == 0

    # A fresh clone - the next run - gets it all back.
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo["remote"]), str(clone)], check=True)
    os.chdir(clone)
    assert vault_cli("pull") == 0
    assert vault_cli("open") == 0
    assert json.loads((clone / "state.json").read_text())["listings"]["x-1"]["title"] == "A car"
    assert (clone / "docs" / "thumbs" / "x-1.webp").read_bytes() == b"photo"


def test_the_vault_branch_is_one_commit_and_holds_no_plaintext(repo):
    work = repo["work"]
    vault_cli("pull")
    vault_cli("open")
    (work / "state.json").write_text('{"listings": {"x-1": {"title": "A car"}}}')
    for _ in range(3):
        vault_cli("seal")
        vault_cli("push")
        (work / "state.json").write_text(
            (work / "state.json").read_text().replace("}}}", ', "n": 1}}}'))
    git(work, "fetch", "-q", "origin", V.VAULT_BRANCH)
    assert git(work, "rev-list", "--count", "FETCH_HEAD") == "1"
    for name in git(work, "ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines():
        blob = subprocess.run(["git", "-C", str(work), "show", f"FETCH_HEAD:{name}"],
                              check=True, capture_output=True).stdout
        assert b"A car" not in blob and b"x-1" not in blob, name


def test_pushing_leaves_main_and_its_index_alone(repo):
    work = repo["work"]
    vault_cli("pull")
    vault_cli("open")
    (work / "state.json").write_text("{}")
    vault_cli("seal")
    vault_cli("push")
    assert git(work, "status", "--porcelain", "--untracked-files=no") == ""
    assert git(work, "ls-files") == "README.md"


def test_a_lease_refuses_to_overwrite_a_newer_save(repo, tmp_path):
    work = repo["work"]
    vault_cli("pull")
    vault_cli("open")
    (work / "state.json").write_text('{"a": 1}')
    vault_cli("seal")
    vault_cli("push")
    vault_cli("pull")                      # this job's base

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(repo["remote"]), str(other)], check=True)
    os.chdir(other)
    vault_cli("pull")
    vault_cli("open")
    (other / "state.json").write_text('{"a": 2}')
    vault_cli("seal")
    vault_cli("push")                      # a check saved in the meantime

    os.chdir(work)
    vault_cli("open")
    (work / "EVENTS.md").write_text("ledger")
    vault_cli("seal")
    assert vault_cli("push", "--lease") != 0


def test_moving_in_from_plaintext_rotates_the_public_topic(repo):
    work = repo["work"]
    cfg = Config.defaults(work / "config.json")
    cfg.set("notifications.channels.ntfy.topic", "the-old-public-topic")
    cfg.set("notifications.channels.ntfy.enabled", True)
    cfg.save()
    vault_cli("pull")
    assert vault_cli("open") == 0
    topic = Config.load(work / "config.json").get("notifications.channels.ntfy.topic")
    assert topic and topic != "the-old-public-topic"

    # A vault that already exists is simply opened: no second move.
    vault_cli("seal")
    vault_cli("push")
    vault_cli("pull")
    vault_cli("open")
    assert Config.load(work / "config.json").get("notifications.channels.ntfy.topic") == topic


def test_the_published_site_is_a_single_commit_of_ciphertext(repo):
    work = repo["work"]
    vault_cli("pull")
    vault_cli("open")
    docs = work / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "index.html").write_text("<!doctype html>")
    (docs / "data.json").write_text('{"listings": [{"title": "A car"}]}')
    assert vault_cli("site", str(work / "site")) == 0
    assert vault_cli("publish", str(work / "site")) == 0
    git(work, "fetch", "-q", "origin", V.SITE_BRANCH)
    names = set(git(work, "ls-tree", "-r", "--name-only", "FETCH_HEAD").splitlines())
    assert {"index.html", "data.enc", "lock.json", ".nojekyll"} <= names
    assert "data.json" not in names


def test_without_a_passphrase_nothing_opens(repo, monkeypatch):
    monkeypatch.delenv(V.ENV_KEY)
    vault_cli("pull")
    assert vault_cli("open") == 2


def envelope(changes, *, at=None, change_id=None):
    """A change the way the dashboard seals it."""
    import secrets
    from datetime import datetime, timezone
    return json.dumps({
        "id": change_id or secrets.token_hex(16),
        "at": at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "changes": changes,
    }).encode()


class TestChangesFromThePage:

    SHORTLIST = [{"action": "shortlist", "listing": "abc-123"}]

    def _setup(self, work):
        cfg = Config.defaults(work / "config.json")
        cfg.add_search("https://www.autotrader.ca/cars/honda/civic/?prx=-2", "Civics")
        cfg.save()
        from autotrader.listing import Listing
        from autotrader.state import State
        state = State(path=work / "state.json")
        state.record(Listing(id="abc-123", url="https://www.autotrader.ca/offers/abc-123",
                             title="A car", price=1000, search_id=cfg.searches[0].id))
        state.save()
        vault = V.Vault.unlock(work, {V.ENV_KEY: PHRASE}, create=True)
        control = work / "control"
        control.mkdir()
        return vault, control

    @staticmethod
    def _drop(control, name, key, body):
        sealed = V.seal(key, body, "control", compress=False, pad=0)
        (control / name).write_text(base64.b64encode(sealed).decode())

    @staticmethod
    def _state(work):
        return json.loads((work / "state.json").read_text())

    def test_a_sealed_change_is_applied_and_recorded(self, repo):
        work = repo["work"]
        vault, control = self._setup(work)
        self._drop(control, "20260924-abc.enc", vault.key, envelope(self.SHORTLIST))
        assert cli.main(["control", "control"]) == 0
        state = self._state(work)
        assert state["listings"]["abc-123"]["you"]["shortlisted"] is True
        assert state["changes"][0]["ok"] is True

    def test_the_same_change_twice_is_refused_the_second_time(self, repo):
        work = repo["work"]
        vault, control = self._setup(work)
        body = envelope(self.SHORTLIST)
        self._drop(control, "a.enc", vault.key, body)
        assert cli.main(["control", "control"]) == 0
        (control / "a.enc").unlink()
        self._drop(control, "b.enc", vault.key, body)      # replayed under a new name
        assert cli.main(["control", "control"]) == 1
        assert "already applied" in self._state(work)["changes"][0]["text"]

    def test_an_old_change_is_refused(self, repo):
        work = repo["work"]
        vault, control = self._setup(work)
        self._drop(control, "a.enc", vault.key,
                   envelope(self.SHORTLIST, at="2020-01-01T00:00:00Z"))
        assert cli.main(["control", "control"]) == 1
        assert not self._state(work)["listings"]["abc-123"].get("you")

    def test_a_sealed_bare_list_is_not_from_the_dashboard(self, repo):
        work = repo["work"]
        vault, control = self._setup(work)
        self._drop(control, "a.enc", vault.key, json.dumps(self.SHORTLIST).encode())
        assert cli.main(["control", "control"]) == 1
        assert not self._state(work)["listings"]["abc-123"].get("you")

    def test_an_unsealed_change_file_is_refused(self, repo):
        """The repository is public: a plain file proves nothing about who wrote it."""
        work = repo["work"]
        _vault, control = self._setup(work)
        (control / "20260924-plain.json").write_text(json.dumps(self.SHORTLIST))
        assert cli.main(["control", "control"]) == 1
        state = self._state(work)
        assert not state["listings"]["abc-123"].get("you")
        assert "sealed" in state["changes"][0]["text"]

    def test_a_refused_change_is_recorded_with_its_reason(self, repo):
        work = repo["work"]
        vault, control = self._setup(work)
        self._drop(control, "a.enc", vault.key,
                   envelope([{"action": "shortlist", "listing": "nope"}]))
        assert cli.main(["control", "control"]) == 1
        state = self._state(work)
        assert state["changes"][0]["ok"] is False
        assert "listing" in state["changes"][0]["text"]

    def test_a_change_sealed_with_another_key_is_refused(self, repo):
        work = repo["work"]
        _vault, control = self._setup(work)
        other = V.subkey(V.derive_key("some other passphrase", b"s" * 16, 1000), "enc")
        self._drop(control, "x.enc", other, envelope(self.SHORTLIST))
        assert cli.main(["control", "control"]) == 1
        assert not self._state(work)["listings"]["abc-123"].get("you")


def test_a_new_passphrase_also_moves_the_topic(repo, monkeypatch):
    """Whoever held the old passphrase could read the topic; they must not
    keep receiving alerts after the change."""
    work = repo["work"]
    vault_cli("pull")
    vault_cli("open")
    cfg = Config.defaults(work / "config.json")
    cfg.set("notifications.channels.ntfy.topic", "autotrader-before")
    cfg.save()
    vault_cli("seal")
    vault_cli("push")

    monkeypatch.setenv(V.ENV_KEY, "an entirely new passphrase")
    monkeypatch.setenv(V.ENV_PREVIOUS, PHRASE)
    vault_cli("pull")
    assert vault_cli("open") == 0
    topic = Config.load(work / "config.json").get("notifications.channels.ntfy.topic")
    assert topic and topic != "autotrader-before"
    vault_cli("seal")
    vault_cli("push")

    # The old passphrase no longer opens it; the new one does, and the topic
    # stays put from then on.
    monkeypatch.delenv(V.ENV_PREVIOUS)
    vault_cli("pull")
    assert vault_cli("open") == 0
    assert Config.load(work / "config.json").get("notifications.channels.ntfy.topic") == topic
    monkeypatch.setenv(V.ENV_KEY, PHRASE)
    vault_cli("pull")
    assert vault_cli("open") == 2


def test_the_vault_commit_carries_no_local_time_zone(repo, monkeypatch):
    monkeypatch.setenv("TZ", "America/Halifax")
    vault_cli("pull")
    vault_cli("open")
    vault_cli("seal")
    vault_cli("push")
    work = repo["work"]
    git(work, "fetch", "-q", "origin", V.VAULT_BRANCH)
    assert git(work, "log", "-1", "--format=%ai %ci", "FETCH_HEAD").count("+0000") == 2


def test_an_outside_timer_is_named_from_the_event_file(tmp_path):
    """Not from the step's environment, which the public log prints."""
    from autotrader.runner import _trigger_of
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"client_payload": {"from": "Laptop"}}))
    env = {"RUN_TRIGGER": "repository_dispatch", "GITHUB_EVENT_PATH": str(event)}
    assert _trigger_of(env) == "repository_dispatch:laptop"
    assert _trigger_of({"RUN_TRIGGER": "repository_dispatch"}) == "repository_dispatch"
