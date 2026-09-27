"""The two things the collector asks of GitHub: the vault's files, and a send.

Both use a fine-grained token limited to this one repository, with Contents
set to read and write - the least that lets it start a workflow.
"""

from __future__ import annotations

from typing import Any

import requests

API = "https://api.github.com"
EVENT = "marketplace"
# What the bot's Status tab calls this timer.
CALLER = "marketplace-collector"


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

    def send(self, sealed: str) -> None:
        """Start the Check workflow with a sealed batch."""
        body: dict[str, Any] = {"event_type": EVENT,
                                "client_payload": {"from": CALLER, "batch": sealed}}
        r = self.http.post(f"{self.api}/repos/{self.repo}/dispatches", json=body,
                           timeout=30)
        if r.status_code != 204:
            raise self._say(r, "sending the batch")

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
