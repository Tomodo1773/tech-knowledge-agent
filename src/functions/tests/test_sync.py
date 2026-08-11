from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from knowledge_agent.contracts import EMBEDDING_DIMENSIONS
from knowledge_agent.github_source import GitTreeEntry
from knowledge_agent.sync import IndexedArticle, SyncPlanningError, build_sync_plan, synchronize

REVISION = "0123456789abcdef0123456789abcdef01234567"
INDEXED_AT = "2026-08-11T00:00:00Z"


def test_builds_blob_chunking_and_deletion_diff() -> None:
    tree = (
        GitTreeEntry("articles/a.md", "a" * 40),
        GitTreeEntry("articles/b.md", "b" * 40),
        GitTreeEntry("articles/c.md", "c" * 40),
        GitTreeEntry("articles/e.md", "e" * 40),
    )
    indexed = (
        IndexedArticle("a", "articles/a.md", REVISION, "a" * 40, "v2"),
        IndexedArticle("b", "articles/b.md", REVISION, "0" * 40, "v2"),
        IndexedArticle("c", "articles/c.md", REVISION, "c" * 40, "v1"),
        IndexedArticle("d", "articles/d.md", REVISION, "d" * 40, "v2"),
    )

    plan = build_sync_plan(tree, indexed, chunking_version="v2")

    assert [entry.article_id for entry in plan.reindex] == ["b", "c", "e"]
    assert plan.delete == ("d",)


def test_rejects_duplicate_slugs_from_nested_source_paths() -> None:
    tree = (
        GitTreeEntry("articles/a.md", "a" * 40),
        GitTreeEntry("articles/nested/a.md", "b" * 40),
    )

    with pytest.raises(SyncPlanningError, match="duplicate source article ID"):
        build_sync_plan(tree, (), chunking_version="v1")


def test_reindexes_manifest_marked_inconsistent_even_when_diff_keys_match() -> None:
    tree = (GitTreeEntry("articles/a.md", "a" * 40),)
    indexed = (
        IndexedArticle(
            "a",
            "articles/a.md",
            REVISION,
            "a" * 40,
            "v1",
            needs_reindex=True,
        ),
    )

    plan = build_sync_plan(tree, indexed, chunking_version="v1")

    assert plan.reindex == tree


@dataclass
class FakeSource:
    head: str = REVISION
    entries: tuple[GitTreeEntry, ...] = (GitTreeEntry("articles/azure.md", "a" * 40),)
    list_calls: int = 0
    fetch_calls: int = 0
    markdown_by_path: dict[str, str] | None = None
    markdown: str = """---
title: Azure
emoji: cloud
type: tech
topics: [azure]
published: true
---
# Intro

Azure article body.
"""

    def get_head_sha(self) -> str:
        return self.head

    def list_articles(self, revision: str) -> tuple[GitTreeEntry, ...]:
        assert revision == self.head
        self.list_calls += 1
        return self.entries

    def fetch_markdown(self, entry: GitTreeEntry, revision: str) -> str:
        assert entry in self.entries
        assert revision == self.head
        self.fetch_calls += 1
        if self.markdown_by_path is not None:
            return self.markdown_by_path[entry.path]
        return self.markdown

    def source_url(self, path: str, revision: str) -> str:
        return f"https://github.com/acme/blog/blob/{revision}/{path}"


@dataclass
class FakeIndex:
    current: tuple[IndexedArticle, ...] = ()
    replaced: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def list_articles(self) -> tuple[IndexedArticle, ...]:
        return self.current

    def replace_article(self, article_id: str, chunks: tuple[object, ...]) -> None:
        self.replaced.append((article_id, chunks))

    def delete_article(self, article_id: str) -> None:
        self.deleted.append(article_id)


@dataclass
class FakeEmbedder:
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return tuple((float(index),) * EMBEDDING_DIMENSIONS for index, _ in enumerate(texts))


def test_synchronizes_article_through_injected_ports() -> None:
    source = FakeSource()
    index = FakeIndex()
    embedder = FakeEmbedder()

    result = synchronize(
        source=source,
        index=index,
        embedder=embedder,
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=None,
    )

    assert result.revision == REVISION
    assert result.indexed == ("azure",)
    assert result.deleted == ()
    assert result.errors == ()
    assert source.fetch_calls == 1
    assert len(embedder.calls) == 1
    article_id, chunks = index.replaced[0]
    assert article_id == "azure"
    assert chunks[0].source_revision == REVISION
    assert chunks[0].source_blob_sha == "a" * 40
    assert chunks[0].source_url.endswith(f"/{REVISION}/articles/azure.md")
    assert len(chunks[0].embedding) == EMBEDDING_DIMENSIONS


def test_matching_head_noops_only_after_tree_and_index_reconcile() -> None:
    source = FakeSource()
    index = FakeIndex(
        current=(
            IndexedArticle("azure", "articles/azure.md", REVISION, "a" * 40, "v1"),
        )
    )
    embedder = FakeEmbedder()

    result = synchronize(
        source=source,
        index=index,
        embedder=embedder,
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=REVISION,
    )

    assert result.unchanged is True
    assert result.aborted is False
    assert source.list_calls == 1
    assert source.fetch_calls == 0
    assert embedder.calls == []


def test_matching_head_reconciles_residue_from_interrupted_and_reverted_sync() -> None:
    source = FakeSource()
    index = FakeIndex(
        current=(
            IndexedArticle("azure", "articles/azure.md", REVISION, "a" * 40, "v1"),
            IndexedArticle("residue", "articles/residue.md", REVISION, "b" * 40, "v1"),
        )
    )

    result = synchronize(
        source=source,
        index=index,
        embedder=FakeEmbedder(),
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=REVISION,
    )

    assert result.unchanged is False
    assert result.deleted == ("residue",)
    assert index.deleted == ["residue"]


def test_matching_head_does_not_reembed_for_stale_source_revision_alone() -> None:
    source = FakeSource()
    index = FakeIndex(
        current=(
            IndexedArticle("azure", "articles/azure.md", "f" * 40, "a" * 40, "v1"),
        )
    )

    embedder = FakeEmbedder()
    result = synchronize(
        source=source,
        index=index,
        embedder=embedder,
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=REVISION,
    )

    assert result.unchanged is True
    assert source.fetch_calls == 0
    assert embedder.calls == []
    assert index.replaced == []


def test_unrelated_commit_with_same_blobs_does_not_reembed_articles() -> None:
    new_revision = "e" * 40
    source = FakeSource(head=new_revision)
    index = FakeIndex(
        current=(
            IndexedArticle("azure", "articles/azure.md", REVISION, "a" * 40, "v1"),
        )
    )
    embedder = FakeEmbedder()

    result = synchronize(
        source=source,
        index=index,
        embedder=embedder,
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=REVISION,
    )

    assert result.unchanged is True
    assert source.list_calls == 1
    assert source.fetch_calls == 0
    assert embedder.calls == []
    assert index.replaced == []


def test_invalid_changed_article_preserves_old_chunks_and_deletion_candidates() -> None:
    source = FakeSource(markdown="---\ntitle: Missing fields\n---\nBody")
    index = FakeIndex(
        current=(
            IndexedArticle("azure", "articles/azure.md", REVISION, "0" * 40, "v1"),
            IndexedArticle("removed", "articles/removed.md", REVISION, "b" * 40, "v1"),
        )
    )
    embedder = FakeEmbedder()

    result = synchronize(
        source=source,
        index=index,
        embedder=embedder,
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=None,
    )

    assert result.indexed == ()
    assert result.deleted == ()
    assert result.aborted is True
    assert result.errors[0].path == "articles/azure.md"
    assert result.errors[0].blocks_sync is True
    assert index.replaced == []
    assert index.deleted == []
    assert embedder.calls == []


def test_invalid_new_article_records_error_and_continues_other_reconcile() -> None:
    valid = FakeSource().markdown
    source = FakeSource(
        entries=(
            GitTreeEntry("articles/new-invalid.md", "b" * 40),
            GitTreeEntry("articles/valid.md", "c" * 40),
        ),
        markdown_by_path={
            "articles/new-invalid.md": "---\ntitle: Missing fields\n---\nBody",
            "articles/valid.md": valid,
        },
    )
    index = FakeIndex(
        current=(
            IndexedArticle("removed", "articles/removed.md", REVISION, "d" * 40, "v1"),
        )
    )

    result = synchronize(
        source=source,
        index=index,
        embedder=FakeEmbedder(),
        chunking_version="v1",
        indexed_at=INDEXED_AT,
        last_successful_sha=None,
    )

    assert result.aborted is False
    assert result.indexed == ("valid",)
    assert result.deleted == ("removed",)
    assert result.errors[0].path == "articles/new-invalid.md"
    assert result.errors[0].blocks_sync is False
    assert [article_id for article_id, _ in index.replaced] == ["valid"]
    assert index.deleted == ["removed"]
