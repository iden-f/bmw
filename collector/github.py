"""The three things the collector asks of GitHub: the vault's files, a
send, and room for a batch too big for a send to carry.

All use a fine-grained token limited to this one repository, with Contents
set to read and write - the least that lets it start a workflow.

A repository_dispatch carries at most 65,535 characters. Five searches of
Marketplace read a thousand cars, and their photos do not fit: the first
version trimmed photos, then cars, to fit, and most cars' photos never
arrived. A big batch now goes up as a git blob (a loose object in the
repository, reachable by its hash, up to 100 MB) and the dispatch carries
the hash; the bot reads the blob back with the workflow's own token. The
blob is sealed like a batch sent inline, so a public repository shows
nothing. Nothing references it, so git's housekeeping drops it in time.
"""

from __future__ import annotations

import re
from typing import Any

import requests

from autotrader.marketplace import PAYLOAD_LIMIT

API = "https://api.github.com"
EVENT = "marketplace"
# What the bot's Status tab calls this timer.
CALLER = "marketplace-collector"
_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str, token: str, *, api: str = API,
                 session: requests.Session | None = None) -> None:
        if not repo or "/" not in repo:
            raise GitHubError("no repository: run collector/run setup")
        if not token:
            raise GitHubError("no GitHub token: run collector/run setup")
        self.repo, self.api = repo, api.rstrip("/")
        self.http = session or requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}",
                                  "X-GitHub-Api-Version": "2022-11-28",
                                  "User-Agent": "autotrader-watch-collector"})

    def _say(self, response: requests.Response, what: str) -> GitHubError:
        hint = {401: "the token was refused - it may have expired",
                403: "the token may not do this - it needs Contents: read and write",
                404: "not found - check the repository name and the token's repository"}
        return GitHubError(f"{what}: HTTP {response.status_code}, "
                           f"{hint.get(response.status_code, response.text[:200])}")

    def file(self, path: str, ref: str = "vault") -> bytes:
        """One file off a branch, raw."""
        r = self.http.get(f"{self.api}/repos/{self.repo}/contents/{path}",
                          params={"ref": ref},
                          headers={"Accept": "application/vnd.github.raw"}, timeout=30)
        if r.status_code != 200:
            raise self._say(r, f"reading {path}")
        return r.content

    def upload(self, sealed: str) -> str:
        """Put a sealed batch in the repository as a blob; its hash."""
        r = self.http.post(f"{self.api}/repos/{self.repo}/git/blobs",
                           json={"content": sealed, "encoding": "utf-8"}, timeout=120)
        if r.status_code != 201:
            raise self._say(r, "uploading the batch")
        sha = str((r.json() or {}).get("sha") or "")
        if not _SHA.match(sha):
            raise GitHubError("uploading the batch: GitHub gave no hash back")
        return sha

    def send(self, sealed: str) -> dict[str, Any]:
        """Start the Check workflow with a sealed batch.

        A batch a dispatch can carry goes in it; a bigger one goes up as a
        blob first, and the dispatch names it. Says which, for the log.
        """
        payload: dict[str, Any] = {"from": CALLER}
        if len(sealed) > PAYLOAD_LIMIT:
            payload["blob"] = self.upload(sealed)
            payload["bytes"] = len(sealed)
        else:
            payload["batch"] = sealed
        body: dict[str, Any] = {"event_type": EVENT, "client_payload": payload}
        r = self.http.post(f"{self.api}/repos/{self.repo}/dispatches", json=body,
                           timeout=30)
        if r.status_code != 204:
            raise self._say(r, "sending the batch")
        return {"blob": payload.get("blob"), "bytes": len(sealed)}

    def check(self) -> str:
        """Whether the token reaches the repository, in words."""
        r = self.http.get(f"{self.api}/repos/{self.repo}", timeout=30)
        if r.status_code != 200:
            raise self._say(r, f"reaching {self.repo}")
        allowed = (r.json().get("permissions") or {})
        if allowed and not allowed.get("push"):
            raise GitHubError("the token can read the repository but not start "
                              "its workflows: give it Contents: read and write")
        return f"the token reaches {self.repo}"
