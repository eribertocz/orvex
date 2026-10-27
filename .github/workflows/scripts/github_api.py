"""Minimal GitHub REST client for repository automation (standard library only)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any, Protocol

API_ROOT = "https://api.github.com"
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubApiError(RuntimeError):
    """Raised when a GitHub REST call fails."""


class GitHubReader(Protocol):
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any: ...

    def paginate(self, path: str) -> Iterator[Any]: ...


class GitHubClient:
    def __init__(self, token: str, repository: str) -> None:
        self._token = token
        self.repository = repository

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = path if path.startswith("https://") else f"{API_ROOT}/repos/{self.repository}{path}"
        response_body, _ = self._send(method, url, body)
        return _decode(response_body, url) if response_body else None

    def paginate(self, path: str) -> Iterator[Any]:
        separator = "&" if "?" in path else "?"
        url: str | None = f"{API_ROOT}/repos/{self.repository}{path}{separator}per_page=100"
        while url is not None:
            response_body, link_header = self._send("GET", url, None)
            yield from _decode(response_body, url)
            match = _NEXT_LINK.search(link_header)
            url = match.group(1) if match else None

    def _send(self, method: str, url: str, body: dict[str, Any] | None) -> tuple[str, str]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode(), response.headers.get("Link", "")
        except urllib.error.HTTPError as error:
            raise GitHubApiError(f"{method} {url} failed with HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise GitHubApiError(f"{method} {url} failed: {error}") from error


def _decode(body: str, url: str) -> Any:
    try:
        return json.loads(body)
    except ValueError as error:
        raise GitHubApiError(f"{url} returned invalid JSON") from error
