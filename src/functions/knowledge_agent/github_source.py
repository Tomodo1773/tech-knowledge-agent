"""GitHub source boundaries and response validation for knowledge synchronization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote

from opentelemetry.trace import SpanKind

from knowledge_agent.telemetry import (
    SPAN_CONTENTS_FETCH,
    SPAN_TREE_FETCH,
    set_attributes,
    traced,
)

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class GitHubSourceError(ValueError):
    """Raised when GitHub source configuration or a response is unsafe or malformed."""


class GitHubTransport(Protocol):
    """Minimal injected HTTP transport implemented by the timer-only runtime."""

    def get_json(self, url: str) -> Any: ...

    def get_text(self, url: str) -> str: ...


def _require_git_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _GIT_SHA_PATTERN.fullmatch(value):
        raise GitHubSourceError(f"{field} must be a 40-character lowercase Git SHA")
    return value


def _is_article_path(path: str) -> bool:
    if not isinstance(path, str):
        return False
    value = PurePosixPath(path)
    return (
        not value.is_absolute()
        and ".." not in value.parts
        and len(value.parts) >= 2
        and value.parts[0] == "articles"
        and value.suffix == ".md"
    )


@dataclass(frozen=True, slots=True)
class GitTreeEntry:
    path: str
    blob_sha: str

    def __post_init__(self) -> None:
        if not _is_article_path(self.path):
            raise GitHubSourceError(f"not an article Markdown path: {self.path!r}")
        _require_git_sha(self.blob_sha, "blob SHA")

    @property
    def article_id(self) -> str:
        return PurePosixPath(self.path).stem


class GitHubSourceClient:
    """Build authenticated GitHub API requests while keeping transport injectable."""

    def __init__(
        self,
        owner: str,
        repository: str,
        default_branch: str,
        transport: GitHubTransport,
    ) -> None:
        for field, value in (("owner", owner), ("repository", repository)):
            if not isinstance(value, str) or not _REPOSITORY_IDENTIFIER_PATTERN.fullmatch(value):
                raise GitHubSourceError(f"invalid GitHub {field}")
        if not isinstance(default_branch, str) or not default_branch.strip():
            raise GitHubSourceError("default branch must not be empty")
        self._owner = owner
        self._repository = repository
        self._default_branch = default_branch
        self._transport = transport

    def get_head_sha(self) -> str:
        branch = quote(self._default_branch, safe="")
        response = self._transport.get_json(
            f"https://api.github.com/repos/{self._owner}/{self._repository}/commits/{branch}"
        )
        if not isinstance(response, Mapping):
            raise GitHubSourceError("GitHub commit response must be an object")
        return _require_git_sha(response.get("sha"), "head SHA")

    def list_articles(self, revision: str) -> tuple[GitTreeEntry, ...]:
        revision = _require_git_sha(revision, "revision")
        url = (
            f"https://api.github.com/repos/{self._owner}/{self._repository}"
            f"/git/trees/{revision}?recursive=1"
        )
        with traced(SPAN_TREE_FETCH, kind=SpanKind.CLIENT) as span:
            response = self._transport.get_json(url)
            entries = self._tree_entries(response)
            set_attributes(span, **{"knowledge.article_count": len(entries)})
            return entries

    @staticmethod
    def _tree_entries(response: Any) -> tuple[GitTreeEntry, ...]:
        if not isinstance(response, Mapping):
            raise GitHubSourceError("GitHub tree response must be an object")
        if response.get("truncated") is not False:
            raise GitHubSourceError("GitHub tree response is truncated or incomplete")
        raw_tree = response.get("tree")
        if not isinstance(raw_tree, list):
            raise GitHubSourceError("GitHub tree response must contain a tree array")

        entries: list[GitTreeEntry] = []
        seen_paths: set[str] = set()
        for item in raw_tree:
            if not isinstance(item, Mapping):
                raise GitHubSourceError("GitHub tree entries must be objects")
            if item.get("type") != "blob":
                continue
            path = item.get("path")
            if not isinstance(path, str):
                raise GitHubSourceError("GitHub blob entries must contain a path")
            if not _is_article_path(path):
                continue
            if path in seen_paths:
                raise GitHubSourceError(f"GitHub tree contains duplicate path: {path}")
            seen_paths.add(path)
            blob_sha = _require_git_sha(item.get("sha"), "blob SHA")
            entries.append(GitTreeEntry(path=path, blob_sha=blob_sha))
        return tuple(sorted(entries, key=lambda entry: entry.path))

    def fetch_markdown(self, entry: GitTreeEntry, revision: str) -> str:
        # The blob SHA already identifies the exact content the tree listed, so the
        # revision only has to be well formed. Private repositories are not readable
        # through raw.githubusercontent.com, so this goes through the Git blobs API.
        _require_git_sha(revision, "revision")
        url = (
            f"https://api.github.com/repos/{self._owner}/{self._repository}"
            f"/git/blobs/{entry.blob_sha}"
        )
        with traced(SPAN_CONTENTS_FETCH, kind=SpanKind.CLIENT):
            content = self._transport.get_text(url)
            if not isinstance(content, str):
                raise GitHubSourceError("GitHub content response must be text")
            return content

    def source_url(self, path: str, revision: str) -> str:
        if not _is_article_path(path):
            raise GitHubSourceError(f"not an article Markdown path: {path!r}")
        revision = _require_git_sha(revision, "revision")
        return (
            f"https://github.com/{self._owner}/{self._repository}/blob/"
            f"{revision}/{quote(path, safe='/')}"
        )
