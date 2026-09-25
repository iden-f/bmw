"""Private mode: everything personal encrypted under one passphrase.

The repository is public, so nothing personal may sit in it in the clear:
not the searches, the cars or the ntfy topic. With the WATCH_PASSPHRASE
secret set, the bot keeps those files encrypted in ``vault/`` and decrypts
them into the working tree only for the length of a run. The published
dashboard uses the same encryption and asks for the passphrase in the
browser.

The format is chosen so a browser can open it with WebCrypto alone:

* key:  PBKDF2-HMAC-SHA256(passphrase, salt, iterations) -> 32 bytes
* blob: b"ATW" | version (1 byte) | flags (1 byte) | nonce (12) | AES-256-GCM
        ciphertext with its tag appended
* flags bit 0: the plaintext was gzip-compressed before encryption
* AAD: the logical name of the file, so one encrypted file cannot be passed
       off as another

``vault/meta.json`` is public by design: the salt, the iteration count and a
check value that confirms a passphrase is right without decrypting anything
else.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAGIC = b"ATW"
VERSION = 1
FLAG_GZIP = 0x01
FLAG_PADDED = 0x02
# Every file is padded to a multiple of one of these before it is encrypted,
# so a file's size says nothing precise about what is in it. Photos use the
# large one: a photo's exact size would otherwise match the public original.
PAD_SMALL = 4_096
PAD_LARGE = 65_536
NONCE_BYTES = 12
ITERATIONS = 600_000          # OWASP's figure for PBKDF2-HMAC-SHA256
MIN_PASSPHRASE = 12
CHECK_PLAINTEXT = b"autotrader-vault-check"

ENV_KEY = "WATCH_PASSPHRASE"
ENV_PREVIOUS = "WATCH_PASSPHRASE_PREVIOUS"

VAULT_DIR = Path("vault")
META_FILE = "meta.json"
THUMBS_DIR = "thumbs"
PAGE_DIGEST = "page.enc"

#: Where the vault and the published site live: single-commit branches,
#: replaced on every write, so neither grows the repository's history.
VAULT_BRANCH = "vault"
SITE_BRANCH = "gh-pages"

#: Working-tree files that hold personal data, and the name each is stored
#: under in the vault. Decrypted for a run, sealed afterwards, and never
#: committed in the clear in private mode.
PRIVATE_FILES = {
    "config.json": "config.enc",
    "state.json": "state.enc",
    "EVENTS.md": "events-ledger.enc",
    "docs/events.json": "events.enc",
    "run.log": "last-run-log.enc",
    "validation-report.md": "validation-report.enc",
}
#: Working-tree directories sealed file by file under hashed names.
PRIVATE_DIRS = {
    "docs/thumbs": "thumbs",
    "diagnostics": "diagnostics",
    "archives": "archives",
}
#: Files a private-mode repository must not contain at all: they hold only
#: personal data, which the encrypted dashboard carries instead.
RETIRED_FILES = ("NOTIFY.md", "SOAK.md", "docs/data.json")

#: The page itself - the only plaintext the published site carries.
SITE_FILES = {"index.html", "app.js", "sw.js", "manifest.webmanifest"}


class VaultError(RuntimeError):
    """The vault could not be opened or written. The message is safe to log."""


# ------------------------------------------------------------------ crypto

def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:                           # pragma: no cover
        raise VaultError("private mode needs the 'cryptography' package - "
                         "pip install -r requirements.txt") from exc
    return AESGCM(key)


def derive_key(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    """The master key. Never used directly: see ``subkey``."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                               int(iterations), dklen=32)


def subkey(master: bytes, purpose: str) -> bytes:
    """Derive the key for one job (encrypting, or naming files).

    HMAC-SHA256 of the master key, which the browser can reproduce with
    WebCrypto.
    """
    return hmac.new(master, f"autotrader-vault/{purpose}".encode("utf-8"),
                    hashlib.sha256).digest()


def _pad(body: bytes, bucket: int) -> bytes:
    """Length-prefix ``body`` and fill it with zeros to a multiple of ``bucket``."""
    used = len(body) + 4
    size = -(-used // bucket) * bucket
    return len(body).to_bytes(4, "big") + body + bytes(size - used)


def seal(key: bytes, plaintext: bytes, name: str, *, compress: bool = True,
         pad: int | None = None) -> bytes:
    """Encrypt ``plaintext`` as the file ``name``. ``pad`` is the size bucket:
    PAD_SMALL for compressed text and PAD_LARGE for binaries by default, 0
    for none."""
    if pad is None:
        pad = PAD_SMALL if compress else PAD_LARGE
    flags = (FLAG_GZIP if compress else 0) | (FLAG_PADDED if pad else 0)
    body = gzip.compress(plaintext, mtime=0) if compress else plaintext
    if pad:
        body = _pad(body, pad)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = _aesgcm(key).encrypt(nonce, body, name.encode("utf-8"))
    return MAGIC + bytes([VERSION, flags]) + nonce + ct


def unseal(key: bytes, blob: bytes, name: str) -> bytes:
    if len(blob) < 5 + NONCE_BYTES + 16 or blob[:3] != MAGIC:
        raise VaultError(f"{name}: not a vault file")
    if blob[3] != VERSION:
        raise VaultError(f"{name}: vault format {blob[3]} is newer than this bot")
    flags = blob[4]
    nonce = blob[5:5 + NONCE_BYTES]
    try:
        body = _aesgcm(key).decrypt(nonce, blob[5 + NONCE_BYTES:], name.encode("utf-8"))
    except Exception as exc:                  # InvalidTag: wrong key or tampered
        raise VaultError(f"{name}: could not be decrypted - wrong passphrase, "
                         f"or the file was altered") from exc
    if flags & FLAG_PADDED:
        used = int.from_bytes(body[:4], "big")
        if used > len(body) - 4:
            raise VaultError(f"{name}: damaged padding")
        body = body[4:4 + used]
    return gzip.decompress(body) if flags & FLAG_GZIP else body


def _refuse_weak(phrase: str) -> None:
    """Refuse an obviously guessable new passphrase. lock.json is public, so
    anyone can test guesses offline; only the passphrase stands in the way.
    Checked when a vault is created or re-keyed, never on an ordinary run."""
    if len(set(phrase.lower())) < 6:
        raise VaultError(f"{ENV_KEY} repeats too few characters to be safe - "
                         f"use four or more random words")


def hashed_name(name_key: bytes, name: str) -> str:
    """A stable file name that reveals nothing about ``name``.

    A listing id is enough to find the car (autotrader.ca/offers/<id>), so
    photos and captures cannot be stored under names built from one.
    """
    return hmac.new(name_key, name.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ------------------------------------------------------------------ vault

@dataclass
class Vault:
    root: Path
    master: bytes
    meta: dict
    #: Set when unlocking re-encrypted the vault under a new passphrase.
    rekeyed: bool = False

    @property
    def key(self) -> bytes:
        return subkey(self.master, "enc")

    @property
    def name_key(self) -> bytes:
        return subkey(self.master, "name")

    # -- lifecycle ----------------------------------------------------------

    @staticmethod
    def passphrase(env: dict[str, str] | None = None) -> str:
        env = os.environ if env is None else env
        return str(env.get(ENV_KEY) or "")

    @classmethod
    def enabled(cls, env: dict[str, str] | None = None) -> bool:
        return bool(cls.passphrase(env))

    @staticmethod
    def exists(root: Path | str = ".") -> bool:
        return (Path(root) / VAULT_DIR / META_FILE).is_file()

    @classmethod
    def unlock(cls, root: Path | str = ".", env: dict[str, str] | None = None,
               *, create: bool = False) -> "Vault":
        """Open the vault for ``root``, keyed from the environment.

        With ``create``, a repository without a vault gets one: a fresh salt,
        written to ``vault/meta.json``. Without it, a missing vault is an
        error, because silently falling back to plain files would publish
        exactly what private mode exists to hide.
        """
        env = os.environ if env is None else env
        root = Path(root)
        phrase = cls.passphrase(env)
        if not phrase:
            raise VaultError(f"{ENV_KEY} is not set")
        if len(phrase) < MIN_PASSPHRASE:
            raise VaultError(f"{ENV_KEY} must be at least {MIN_PASSPHRASE} "
                             f"characters - it is the only thing between the "
                             f"public and everything this bot records")
        meta_path = root / VAULT_DIR / META_FILE
        if not meta_path.is_file():
            if not create:
                raise VaultError("this repository has no vault yet")
            _refuse_weak(phrase)
            salt = secrets.token_bytes(16)
            master = derive_key(phrase, salt)
            meta = {"format": "autotrader-vault", "version": VERSION,
                    "kdf": {"name": "PBKDF2", "hash": "SHA-256",
                            "iterations": ITERATIONS, "salt": _b64(salt)},
                    "check": _b64(seal(subkey(master, "enc"), CHECK_PLAINTEXT,
                                       "check", compress=False, pad=0))}
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            return cls(root, master, meta)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        kdf = meta.get("kdf") or {}
        salt = _unb64(str(kdf.get("salt") or ""))
        iterations = int(kdf.get("iterations") or ITERATIONS)
        master = derive_key(phrase, salt, iterations)
        if cls._checks(master, meta):
            return cls(root, master, meta)
        previous = str(env.get(ENV_PREVIOUS) or "")
        if previous:
            old = derive_key(previous, salt, iterations)
            if cls._checks(old, meta):
                vault = cls(root, old, meta)
                _refuse_weak(phrase)
                vault.rekey(phrase)
                vault.rekeyed = True
                return vault
        raise VaultError(f"{ENV_KEY} does not open this vault. If you changed "
                         f"it, set the old one as {ENV_PREVIOUS} for one run.")

    @staticmethod
    def _checks(master: bytes, meta: dict) -> bool:
        try:
            return unseal(subkey(master, "enc"), _unb64(str(meta.get("check") or "")),
                          "check") == CHECK_PLAINTEXT
        except VaultError:
            return False

    @property
    def dir(self) -> Path:
        return self.root / VAULT_DIR

    # -- files --------------------------------------------------------------

    def _write(self, path: Path, blob: bytes) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return True

    def seal_file(self, source: Path, stored_as: str, *, compress: bool = True) -> bool:
        """Seal ``source`` unless the vault already holds the same plaintext.

        AES-GCM output differs every time, so re-sealing an unchanged file
        would make a new blob and a new commit on every run. Comparing
        plaintexts keeps an unchanged file unchanged.
        """
        data = source.read_bytes()
        target = self.dir / stored_as
        if target.is_file():
            try:
                if unseal(self.key, target.read_bytes(), stored_as) == data:
                    return False
            except VaultError:
                pass
        return self._write(target, seal(self.key, data, stored_as, compress=compress))

    def open_file(self, stored_as: str, target: Path) -> bool:
        source = self.dir / stored_as
        if not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(unseal(self.key, source.read_bytes(), stored_as))
        return True

    def _index_name(self, folder: str) -> str:
        return f"{folder}/index.enc"

    def _dir_index(self, folder: str) -> dict[str, str]:
        index_path = self.dir / self._index_name(folder)
        if not index_path.is_file():
            return {}
        return json.loads(unseal(self.key, index_path.read_bytes(),
                                 self._index_name(folder)))

    def seal_dir(self, source: Path, folder: str) -> int:
        """Seal every file under ``source`` into ``vault/<folder>/``.

        Each file is stored under a hashed name, and an encrypted index maps
        the hashed names back to the real ones. Files are write-once in
        practice (a photo, a capture), so an existing sealed copy is kept.
        """
        index = self._dir_index(folder)
        changed = 0
        if source.is_dir():
            for path in sorted(p for p in source.rglob("*") if p.is_file()):
                rel = path.relative_to(source).as_posix()
                hashed = hashed_name(self.name_key, f"{folder}/{rel}")
                stored = f"{folder}/{hashed}.bin"
                binary = not rel.endswith((".json", ".md", ".txt", ".html", ".log"))
                if self.seal_file(path, stored, compress=not binary):
                    changed += 1
                if index.get(hashed) != rel:
                    index[hashed] = rel
                    changed += 1
        # Drop sealed copies of files missing from the working tree, so a
        # pruned photo does not stay in the vault.
        live = {hashed_name(self.name_key, f"{folder}/{rel}")
                for rel in (p.relative_to(source).as_posix()
                            for p in source.rglob("*") if p.is_file())} \
            if source.is_dir() else set()
        for hashed in list(index):
            if hashed not in live:
                (self.dir / folder / f"{hashed}.bin").unlink(missing_ok=True)
                index.pop(hashed)
                changed += 1
        if index or (self.dir / self._index_name(folder)).exists():
            blob = json.dumps(index, sort_keys=True).encode("utf-8")
            if self.seal_file_bytes(blob, self._index_name(folder)):
                changed += 1
        return changed

    def seal_file_bytes(self, data: bytes, stored_as: str) -> bool:
        target = self.dir / stored_as
        if target.is_file():
            try:
                if unseal(self.key, target.read_bytes(), stored_as) == data:
                    return False
            except VaultError:
                pass
        return self._write(target, seal(self.key, data, stored_as))

    def open_dir(self, folder: str, target: Path) -> int:
        opened = 0
        for hashed, rel in self._dir_index(folder).items():
            stored = f"{folder}/{hashed}.bin"
            if self.open_file(stored, target / rel):
                opened += 1
        return opened

    # -- whole-repository operations ------------------------------------------

    def open_all(self, *, include_log: bool = False) -> dict[str, int]:
        """Decrypt everything into the working tree for a run.

        The last run's log is left sealed unless asked for: a run writes its
        own, and the log is only ever read by a person.
        """
        opened = {}
        for plain, stored in PRIVATE_FILES.items():
            if plain == "run.log" and not include_log:
                continue
            opened[plain] = int(self.open_file(stored, self.root / plain))
        for plain, folder in PRIVATE_DIRS.items():
            opened[plain] = self.open_dir(folder, self.root / plain)
        return opened

    def seal_all(self) -> int:
        """Encrypt everything back into ``vault/``. Returns files changed."""
        changed = 0
        for plain, stored in PRIVATE_FILES.items():
            path = self.root / plain
            if path.is_file():
                changed += int(self.seal_file(path, stored))
        for plain, folder in PRIVATE_DIRS.items():
            changed += self.seal_dir(self.root / plain, folder)
        return changed

    def page_changed(self, data_json: Path) -> bool:
        """Report whether the dashboard's data changed since the last publish.

        Its own timestamp is ignored. The new digest is recorded on a change.
        """
        if not data_json.is_file():
            return False
        data = json.loads(data_json.read_text(encoding="utf-8"))
        data.pop("generated_at", None)
        digest = hashlib.sha256(
            json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest().encode()
        return self.seal_file_bytes(digest, PAGE_DIGEST)

    def rekey(self, new_passphrase: str) -> None:
        """Re-encrypt the whole vault under a new passphrase."""
        files: dict[str, bytes] = {}
        for path in sorted(p for p in self.dir.rglob("*.enc")) + \
                sorted(p for p in self.dir.rglob("*.bin")):
            stored = path.relative_to(self.dir).as_posix()
            files[stored] = unseal(self.key, path.read_bytes(), stored)
        salt = secrets.token_bytes(16)
        self.master = derive_key(new_passphrase, salt)
        self.meta["kdf"]["salt"] = _b64(salt)
        self.meta["kdf"]["iterations"] = ITERATIONS
        self.meta["check"] = _b64(seal(self.key, CHECK_PLAINTEXT, "check", compress=False, pad=0))
        (self.dir / META_FILE).write_text(json.dumps(self.meta, indent=2) + "\n",
                                          encoding="utf-8")
        # Hashed names depend on the key, so directory contents move.
        indexes = {stored.rsplit("/", 1)[0]: json.loads(data)
                   for stored, data in files.items() if stored.endswith("/index.enc")}
        for stored, data in files.items():
            folder = stored.rsplit("/", 1)[0] if "/" in stored else ""
            if folder in indexes:
                continue
            (self.dir / stored).write_bytes(seal(self.key, data, stored))
        for folder, index in indexes.items():
            new_index = {}
            for hashed, rel in index.items():
                old = self.dir / folder / f"{hashed}.bin"
                data = files.get(f"{folder}/{hashed}.bin")
                old.unlink(missing_ok=True)
                if data is None:
                    continue
                fresh = hashed_name(self.name_key, f"{folder}/{rel}")
                stored = f"{folder}/{fresh}.bin"
                binary = not rel.endswith((".json", ".md", ".txt", ".html", ".log"))
                (self.dir / stored).write_bytes(
                    seal(self.key, data, stored, compress=not binary))
                new_index[fresh] = rel
            self.seal_file_bytes(json.dumps(new_index, sort_keys=True).encode("utf-8"),
                                 self._index_name(folder))

    # -- the published site ---------------------------------------------------

    def publish(self, docs: Path, out: Path) -> dict[str, int]:
        """Build the site to publish in ``out``: the page, and only ciphertext.

        ``docs/`` holds the page and, after a run, the plaintext it is built
        from. Only the page's own files are copied; data.json goes out as
        data.enc and each photo as thumbs/<hashed>.bin. Everything else in
        ``docs/`` is left behind.
        """
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        copied = 0
        for path in sorted(p for p in docs.iterdir() if p.is_file()):
            if path.name not in SITE_FILES and not path.name.startswith("icon"):
                continue
            shutil.copy2(path, out / path.name)
            copied += 1
        sealed = 0
        if (docs / "data.json").is_file():
            (out / "data.enc").write_bytes(
                seal(self.key, (docs / "data.json").read_bytes(), "data.json",
                     pad=PAD_LARGE))
            sealed = 1
        photos = 0
        thumbs = docs / "thumbs"
        if thumbs.is_dir():
            (out / "thumbs").mkdir()
            for path in sorted(p for p in thumbs.iterdir() if p.is_file()):
                if path.name == "index.json":
                    continue
                hashed = self.photo_name(path.name)
                (out / "thumbs" / f"{hashed}.bin").write_bytes(
                    seal(self.key, path.read_bytes(), f"thumbs/{hashed}.bin",
                         compress=False))
                photos += 1
        lock = {k: self.meta[k] for k in ("format", "version", "kdf", "check")}
        (out / "lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
        return {"copied": copied, "sealed": sealed, "photos": photos}

    def photo_name(self, filename: str) -> str:
        return hashed_name(self.name_key, f"photo/{filename}")


# ------------------------------------------------------------------ branches

def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, env=env)
    if done.returncode != 0:
        raise VaultError(f"git {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def pull_branch(root: Path | str, branch: str, into: Path | str) -> str | None:
    """Replace ``into`` with the files on ``branch`` at origin.

    Returns the commit fetched, or None when the branch does not exist yet
    (a repository's first run), in which case ``into`` is left empty.
    """
    root, into = Path(root), Path(into)
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    if not _git(root, "ls-remote", "--heads", "origin", branch):
        return None
    _git(root, "fetch", "-q", "--depth", "1", "origin", branch)
    commit = _git(root, "rev-parse", "FETCH_HEAD")
    archive = subprocess.run(["git", "-C", str(root), "archive", commit],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        try:
            tar.extractall(into, filter="data")
        except TypeError:                                  # Python < 3.11.4
            tar.extractall(into)
    return commit


def push_branch(root: Path | str, branch: str, source: Path | str, message: str,
                *, lease: str | None = None) -> str:
    """Make ``branch`` at origin a single commit holding exactly ``source``.

    Built with a throwaway index, so the working tree and the index of the
    branch that is checked out are never touched. With ``lease`` (the commit
    ``pull_branch`` returned, or "" for a branch that did not exist) the push
    is refused if the branch was written in the meantime.
    """
    root, source = Path(root), Path(source).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        env.setdefault("GIT_AUTHOR_NAME", "autotrader-bot")
        env.setdefault("GIT_AUTHOR_EMAIL",
                       "41898282+github-actions[bot]@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        # The commit is public; a local clock's time zone would say where
        # the owner is.
        env["TZ"] = "UTC"
        _git(root, f"--work-tree={source}", "add", "-A", env=env)
        tree = _git(root, "write-tree", env=env)
        commit = _git(root, "commit-tree", tree, "-m", message, env=env)
    force = "--force" if lease is None else f"--force-with-lease=refs/heads/{branch}:{lease}"
    _git(root, "push", "-q", force, "origin", f"{commit}:refs/heads/{branch}")
    return commit


# ------------------------------------------------------------------ helpers

def private_paths() -> Iterable[str]:
    yield from PRIVATE_FILES
    yield from PRIVATE_DIRS
    yield from RETIRED_FILES
