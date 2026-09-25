"""Private mode's encryption: the part that must be right for any of it to matter."""
import json

import pytest

from autotrader import vault as V

PHRASE = "correct horse battery staple"
ENV = {V.ENV_KEY: PHRASE}


def make_repo(tmp_path):
    (tmp_path / "config.json").write_text('{"searches": [{"name": "Honda Civic secret"}]}')
    (tmp_path / "state.json").write_text('{"listings": {"abc-123": {"title": "2018 Honda Civic"}}}')
    (tmp_path / "EVENTS.md").write_text("# first price drop on 2018 Honda Civic\n")
    (tmp_path / "docs" / "thumbs").mkdir(parents=True)
    (tmp_path / "docs" / "thumbs" / "abc-123-1.webp").write_bytes(b"RIFF\x00fake-webp")
    (tmp_path / "docs" / "events.json").write_text('[{"title": "2018 Honda Civic"}]')
    (tmp_path / "docs" / "data.json").write_text('{"listings": [{"title": "2018 Honda Civic"}]}')
    (tmp_path / "docs" / "index.html").write_text("<!doctype html><title>Watch</title>")
    return tmp_path


class TestTheCipher:

    def test_round_trip(self):
        key = V.subkey(V.derive_key(PHRASE, b"s" * 16, 1000), "enc")
        blob = V.seal(key, b"hello", "config.json")
        assert blob[:3] == V.MAGIC and b"hello" not in blob
        assert V.unseal(key, blob, "config.json") == b"hello"

    def test_the_wrong_key_is_refused(self):
        key = V.subkey(V.derive_key(PHRASE, b"s" * 16, 1000), "enc")
        other = V.subkey(V.derive_key("another passphrase!", b"s" * 16, 1000), "enc")
        with pytest.raises(V.VaultError):
            V.unseal(other, V.seal(key, b"hello", "x"), "x")

    def test_a_tampered_file_is_refused(self):
        key = V.subkey(V.derive_key(PHRASE, b"s" * 16, 1000), "enc")
        blob = bytearray(V.seal(key, b"hello", "x"))
        blob[-1] ^= 1
        with pytest.raises(V.VaultError):
            V.unseal(key, bytes(blob), "x")

    def test_one_file_cannot_be_passed_off_as_another(self):
        """The file's name is authenticated with it."""
        key = V.subkey(V.derive_key(PHRASE, b"s" * 16, 1000), "enc")
        with pytest.raises(V.VaultError):
            V.unseal(key, V.seal(key, b"{}", "config.json"), "state.json")

    def test_the_two_subkeys_differ(self):
        master = V.derive_key(PHRASE, b"s" * 16, 1000)
        assert V.subkey(master, "enc") != V.subkey(master, "name") != master


class TestUnlocking:

    def test_a_missing_passphrase_is_an_error_not_a_fallback(self, tmp_path):
        with pytest.raises(V.VaultError, match="not set"):
            V.Vault.unlock(tmp_path, {}, create=True)

    def test_a_short_passphrase_is_refused(self, tmp_path):
        with pytest.raises(V.VaultError, match="at least"):
            V.Vault.unlock(tmp_path, {V.ENV_KEY: "short"}, create=True)

    def test_no_vault_is_an_error_unless_creating(self, tmp_path):
        with pytest.raises(V.VaultError, match="no vault"):
            V.Vault.unlock(tmp_path, ENV)
        V.Vault.unlock(tmp_path, ENV, create=True)
        assert V.Vault.exists(tmp_path)
        V.Vault.unlock(tmp_path, ENV)

    def test_the_wrong_passphrase_is_refused(self, tmp_path):
        V.Vault.unlock(tmp_path, ENV, create=True)
        with pytest.raises(V.VaultError, match="does not open"):
            V.Vault.unlock(tmp_path, {V.ENV_KEY: "a different long phrase"})

    def test_meta_holds_no_secret(self, tmp_path):
        V.Vault.unlock(tmp_path, ENV, create=True)
        meta = (tmp_path / "vault" / "meta.json").read_text()
        assert PHRASE not in meta
        assert set(json.loads(meta)) == {"format", "version", "kdf", "check"}


class TestTheRepository:

    def test_seal_then_open_restores_every_file(self, tmp_path):
        repo = make_repo(tmp_path)
        before = {p: (repo / p).read_bytes() for p in
                  ("config.json", "state.json", "EVENTS.md", "docs/events.json",
                   "docs/thumbs/abc-123-1.webp")}
        vault = V.Vault.unlock(repo, ENV, create=True)
        vault.seal_all()
        for p in before:
            (repo / p).unlink()
        V.Vault.unlock(repo, ENV).open_all()
        for p, data in before.items():
            assert (repo / p).read_bytes() == data, p

    def test_nothing_readable_is_left_in_the_vault(self, tmp_path):
        repo = make_repo(tmp_path)
        V.Vault.unlock(repo, ENV, create=True).seal_all()
        for path in (repo / "vault").rglob("*"):
            # meta.json is public by design, and its random salt is base64
            # that can spell anything short.
            if path.is_file() and path.name != "meta.json":
                blob = path.read_bytes()
                # Six bytes or more: a padded 64 KiB blob of random bytes
                # contains any given three-byte string about one time in 250.
                for secret in (b"Honda Civic", b"abc-123", b"secret", b"price drop"):
                    assert secret not in blob, (path, secret)
                assert "abc-123" not in path.name

    def test_an_unchanged_file_is_not_rewritten(self, tmp_path):
        """AES-GCM output differs every time; without this every run would
        commit a new blob for every file."""
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        vault.seal_all()
        snapshot = {p: p.read_bytes() for p in (repo / "vault").rglob("*") if p.is_file()}
        assert vault.seal_all() == 0
        after = {p: p.read_bytes() for p in (repo / "vault").rglob("*") if p.is_file()}
        assert snapshot == after

    def test_a_changed_file_is_rewritten(self, tmp_path):
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        vault.seal_all()
        (repo / "state.json").write_text('{"listings": {}}')
        assert vault.seal_all() == 1

    def test_a_removed_photo_leaves_the_vault(self, tmp_path):
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        vault.seal_all()
        assert len(list((repo / "vault" / "thumbs").glob("*.bin"))) == 1
        (repo / "docs" / "thumbs" / "abc-123-1.webp").unlink()
        vault.seal_all()
        assert list((repo / "vault" / "thumbs").glob("*.bin")) == []

    def test_changing_the_passphrase_with_the_previous_one_set(self, tmp_path):
        repo = make_repo(tmp_path)
        V.Vault.unlock(repo, ENV, create=True).seal_all()
        new = {V.ENV_KEY: "a brand new long passphrase", V.ENV_PREVIOUS: PHRASE}
        V.Vault.unlock(repo, new)
        for p in ("config.json", "state.json", "docs/thumbs/abc-123-1.webp"):
            (repo / p).unlink()
        V.Vault.unlock(repo, {V.ENV_KEY: "a brand new long passphrase"}).open_all()
        assert "secret" in (repo / "config.json").read_text()
        assert (repo / "docs/thumbs/abc-123-1.webp").read_bytes() == b"RIFF\x00fake-webp"
        with pytest.raises(V.VaultError):
            V.Vault.unlock(repo, ENV)


class TestTheSite:

    def test_the_published_site_carries_no_plaintext(self, tmp_path):
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        out = tmp_path / "site"
        vault.publish(repo / "docs", out)
        names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        assert "data.json" not in names and "events.json" not in names
        assert {"data.enc", "lock.json", "index.html"} <= set(names)
        assert not any(n.startswith("thumbs/") and not n.endswith(".bin") for n in names)
        for path in out.rglob("*"):
            if path.is_file() and path.name != "index.html":
                blob = path.read_bytes()
                assert b"Honda Civic" not in blob and b"abc-123" not in blob, path
                assert "abc-123" not in path.name

    def test_what_is_published_opens_with_the_same_key(self, tmp_path):
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        out = tmp_path / "site"
        vault.publish(repo / "docs", out)
        data = V.unseal(vault.key, (out / "data.enc").read_bytes(), "data.json")
        assert json.loads(data)["listings"][0]["title"] == "2018 Honda Civic"
        photo = vault.photo_name("abc-123-1.webp")
        blob = (out / "thumbs" / f"{photo}.bin").read_bytes()
        assert V.unseal(vault.key, blob, f"thumbs/{photo}.bin") == b"RIFF\x00fake-webp"

    def test_the_lock_file_is_the_public_half_of_meta(self, tmp_path):
        repo = make_repo(tmp_path)
        vault = V.Vault.unlock(repo, ENV, create=True)
        out = tmp_path / "site"
        vault.publish(repo / "docs", out)
        lock = json.loads((out / "lock.json").read_text())
        meta = json.loads((repo / "vault" / "meta.json").read_text())
        assert lock == meta


class TestWeakPassphrases:

    def test_a_repetitive_new_passphrase_is_refused(self, tmp_path):
        with pytest.raises(V.VaultError, match="random words"):
            V.Vault.unlock(tmp_path, {V.ENV_KEY: "aaaaaaaaaaaaaaa"}, create=True)

    def test_an_existing_vault_is_never_locked_out_by_the_rule(self, tmp_path):
        V.Vault.unlock(tmp_path, ENV, create=True)
        # Unlocking an existing vault checks the passphrase, not its quality.
        V.Vault.unlock(tmp_path, ENV)

    def test_a_weak_replacement_is_refused_and_the_old_one_still_works(self, tmp_path):
        V.Vault.unlock(tmp_path, ENV, create=True)
        with pytest.raises(V.VaultError, match="random words"):
            V.Vault.unlock(tmp_path, {V.ENV_KEY: "abababababab", V.ENV_PREVIOUS: PHRASE})
        V.Vault.unlock(tmp_path, ENV)
