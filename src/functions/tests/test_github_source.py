from __future__ import annotations

from typing import Any

import pytest

from knowledge_agent.github_source import GitHubSourceClient, GitHubSourceError

REVISION = "0123456789abcdef0123456789abcdef01234567"


class FakeTransport:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get_json(self, url: str) -> Any:
        self.requested.append(url)
        return self.responses[url]

    def get_text(self, url: str) -> str:
        self.requested.append(url)
        return self.responses[url]


def test_fetches_head_filtered_tree_and_revision_fixed_content() -> None:
    commit_url = "https://api.github.com/repos/acme/blog/commits/main"
    tree_url = f"https://api.github.com/repos/acme/blog/git/trees/{REVISION}?recursive=1"
    # Private repositories are read through the blobs API, keyed by the tree blob SHA.
    content_url = f"https://api.github.com/repos/acme/blog/git/blobs/{REVISION}"
    transport = FakeTransport(
        {
            commit_url: {"sha": REVISION},
            tree_url: {
                "truncated": False,
                "tree": [
                    {"path": "articles/azure.md", "type": "blob", "sha": REVISION},
                    {"path": "articles/nested/python.md", "type": "blob", "sha": "a" * 40},
                    {"path": "x-articles/hidden.md", "type": "blob", "sha": "b" * 40},
                    {"path": "articles/image.png", "type": "blob", "sha": "c" * 40},
                ],
            },
            content_url: "---\ntitle: Azure\n---\nbody",
        }
    )
    source = GitHubSourceClient("acme", "blog", "main", transport)

    assert source.get_head_sha() == REVISION
    entries = source.list_articles(REVISION)
    assert [entry.path for entry in entries] == [
        "articles/azure.md",
        "articles/nested/python.md",
    ]
    assert source.fetch_markdown(entries[0], REVISION).endswith("body")
    assert transport.requested == [commit_url, tree_url, content_url]


def test_rejects_truncated_tree_to_prevent_false_deletions() -> None:
    tree_url = f"https://api.github.com/repos/acme/blog/git/trees/{REVISION}?recursive=1"
    source = GitHubSourceClient(
        "acme",
        "blog",
        "main",
        FakeTransport({tree_url: {"truncated": True, "tree": []}}),
    )

    with pytest.raises(GitHubSourceError, match="truncated"):
        source.list_articles(REVISION)


def test_rejects_invalid_revision_before_request() -> None:
    source = GitHubSourceClient("acme", "blog", "main", FakeTransport({}))

    with pytest.raises(GitHubSourceError, match="40-character"):
        source.list_articles("not-a-sha")
