"""GitHub commit polling.

The founder pushes raw commits (usually no tags/releases/PRs), so we poll each
watched repo's commit list since the last cursor. We pull commit messages and
changed file paths (cheap, low secret-surface); full patches are intentionally
not fetched by default. Everything still passes through the scrubber downstream.

`CommitSource` is a Protocol so tests inject a fake without network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .config import Repo


@dataclass
class Commit:
    sha: str
    message: str
    date: str  # ISO 8601
    author: str
    files: list[str] = field(default_factory=list)


class CommitSource(Protocol):
    def list_commits(self, repo: Repo, since: str | None, author: str | None) -> list[Commit]:
        ...

    def list_repos(self, *, since_pushed: str | None = None) -> list[Repo]:
        """Repos owned by the authenticated user, most-recently-pushed first."""
        ...


class GitHubCommitSource:
    """Real source backed by the GitHub REST API."""

    API = "https://api.github.com"

    def __init__(self, token: str | None) -> None:
        self._token = token

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def list_commits(self, repo: Repo, since: str | None, author: str | None) -> list[Commit]:
        import requests  # lazy

        params: dict[str, str] = {"per_page": "50"}
        if since:
            params["since"] = since
        if author:
            params["author"] = author
        url = f"{self.API}/repos/{repo.owner}/{repo.name}/commits"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        commits: list[Commit] = []
        for item in resp.json():
            commit = item.get("commit", {})
            files = self._list_files(repo, item["sha"])
            commits.append(
                Commit(
                    sha=item["sha"],
                    message=commit.get("message", ""),
                    date=commit.get("author", {}).get("date", ""),
                    author=(item.get("author") or {}).get("login")
                    or commit.get("author", {}).get("name", ""),
                    files=files,
                )
            )
        return commits

    def list_repos(self, *, since_pushed: str | None = None) -> list[Repo]:
        """All repos the authenticated user owns (incl. private), pushed-desc.

        Requires a token (uses /user/repos). Stops early once repos fall outside the
        ``since_pushed`` window, since results are sorted by push time.
        """
        import requests  # lazy

        repos: list[Repo] = []
        page = 1
        while True:
            resp = requests.get(
                f"{self.API}/user/repos",
                headers=self._headers(),
                params={"per_page": "100", "page": str(page), "sort": "pushed",
                        "affiliation": "owner"},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for item in batch:
                if since_pushed and (item.get("pushed_at") or "") < since_pushed:
                    return repos  # sorted newest-first -> everything after is older
                repos.append(Repo(owner=item["owner"]["login"], name=item["name"]))
            if len(batch) < 100:
                break
            page += 1
        return repos

    def _list_files(self, repo: Repo, sha: str) -> list[str]:
        import requests  # lazy

        url = f"{self.API}/repos/{repo.owner}/{repo.name}/commits/{sha}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code != 200:
            return []
        return [f.get("filename", "") for f in resp.json().get("files", [])]
