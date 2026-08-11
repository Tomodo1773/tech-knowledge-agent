"""Pure synchronization planning and orchestration over injected external ports."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from knowledge_agent.chunking import Article, ArticleFormatError, chunk_article, parse_article
from knowledge_agent.contracts import CORPUS_ID, CosmosChunk
from knowledge_agent.github_source import GitTreeEntry

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class SyncPlanningError(ValueError):
    """Raised when a source/index snapshot cannot be reconciled safely."""


class SyncSource(Protocol):
    def get_head_sha(self) -> str: ...

    def list_articles(self, revision: str) -> Sequence[GitTreeEntry]: ...

    def fetch_markdown(self, entry: GitTreeEntry, revision: str) -> str: ...

    def source_url(self, path: str, revision: str) -> str: ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: tuple[str, ...]) -> Sequence[Sequence[float]]: ...


class IndexRepository(Protocol):
    def list_articles(self) -> Sequence[IndexedArticle]: ...

    def replace_article(self, article_id: str, chunks: tuple[CosmosChunk, ...]) -> None: ...

    def delete_article(self, article_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class IndexedArticle:
    article_id: str
    source_path: str
    source_revision: str
    source_blob_sha: str
    chunking_version: str
    needs_reindex: bool = False

    def __post_init__(self) -> None:
        for field, value in (
            ("article_id", self.article_id),
            ("source_path", self.source_path),
            ("chunking_version", self.chunking_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SyncPlanningError(f"{field} must not be empty")
        if not _GIT_SHA_PATTERN.fullmatch(self.source_revision):
            raise SyncPlanningError("source_revision must be a lowercase Git SHA")
        if not _GIT_SHA_PATTERN.fullmatch(self.source_blob_sha):
            raise SyncPlanningError("source_blob_sha must be a lowercase Git SHA")
        if not isinstance(self.needs_reindex, bool):
            raise SyncPlanningError("needs_reindex must be a boolean")


@dataclass(frozen=True, slots=True)
class SyncPlan:
    reindex: tuple[GitTreeEntry, ...]
    delete: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArticleSyncError:
    path: str
    message: str
    blocks_sync: bool


@dataclass(frozen=True, slots=True)
class SyncResult:
    revision: str
    unchanged: bool
    aborted: bool = False
    indexed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    errors: tuple[ArticleSyncError, ...] = ()


def build_sync_plan(
    tree: Sequence[GitTreeEntry],
    indexed: Sequence[IndexedArticle],
    *,
    chunking_version: str,
) -> SyncPlan:
    if not isinstance(chunking_version, str) or not chunking_version.strip():
        raise SyncPlanningError("chunking_version must not be empty")

    indexed_by_id: dict[str, IndexedArticle] = {}
    for article in indexed:
        if article.article_id in indexed_by_id:
            raise SyncPlanningError(f"duplicate indexed article ID: {article.article_id}")
        indexed_by_id[article.article_id] = article

    tree_by_id: dict[str, GitTreeEntry] = {}
    for entry in tree:
        if entry.article_id in tree_by_id:
            raise SyncPlanningError(f"duplicate source article ID: {entry.article_id}")
        tree_by_id[entry.article_id] = entry

    reindex: list[GitTreeEntry] = []
    for article_id, entry in sorted(tree_by_id.items()):
        current = indexed_by_id.get(article_id)
        if (
            current is None
            or current.needs_reindex
            or current.source_path != entry.path
            or current.source_blob_sha != entry.blob_sha
            or current.chunking_version != chunking_version
        ):
            reindex.append(entry)
    delete = tuple(sorted(set(indexed_by_id) - set(tree_by_id)))
    return SyncPlan(reindex=tuple(reindex), delete=delete)


def _embedding_text(article: Article, heading: str | None, text: str) -> str:
    prefix = article.title if heading is None else f"{article.title}\n{heading}"
    return f"{prefix}\n\n{text}"


def synchronize(
    *,
    source: SyncSource,
    index: IndexRepository,
    embedder: EmbeddingProvider,
    chunking_version: str,
    indexed_at: str,
    last_successful_sha: str | None,
) -> SyncResult:
    revision = source.get_head_sha()
    if not _GIT_SHA_PATTERN.fullmatch(revision):
        raise SyncPlanningError("source head must be a lowercase Git SHA")
    if last_successful_sha is not None and not _GIT_SHA_PATTERN.fullmatch(
        last_successful_sha
    ):
        raise SyncPlanningError("last successful SHA must be a lowercase Git SHA")

    tree = tuple(source.list_articles(revision))
    current = tuple(index.list_articles())
    plan = build_sync_plan(
        tree,
        current,
        chunking_version=chunking_version,
    )
    current_ids = {article.article_id for article in current}

    parsed: list[tuple[GitTreeEntry, Article]] = []
    errors: list[ArticleSyncError] = []
    for entry in plan.reindex:
        try:
            markdown = source.fetch_markdown(entry, revision)
            parsed.append((entry, parse_article(entry.path, markdown)))
        except ArticleFormatError as error:
            errors.append(
                ArticleSyncError(
                    path=entry.path,
                    message=str(error),
                    blocks_sync=entry.article_id in current_ids,
                )
            )
    if any(error.blocks_sync for error in errors):
        return SyncResult(
            revision=revision,
            unchanged=False,
            aborted=True,
            errors=tuple(errors),
        )

    staged: list[tuple[str, tuple[CosmosChunk, ...]]] = []
    for entry, article in parsed:
        content_chunks = chunk_article(article)
        embedding_texts = tuple(
            _embedding_text(article, chunk.heading, chunk.text) for chunk in content_chunks
        )
        embeddings = tuple(tuple(values) for values in embedder.embed(embedding_texts))
        if len(embeddings) != len(content_chunks):
            raise SyncPlanningError("embedding provider returned a mismatched result count")
        chunks = tuple(
            CosmosChunk(
                id=f"{article.slug}:{chunk.index}",
                corpus_id=CORPUS_ID,
                article_id=article.slug,
                chunk_index=chunk.index,
                slug=article.slug,
                title=article.title,
                emoji=article.emoji,
                article_type=article.article_type,
                topics=article.topics,
                published=article.published,
                published_at=article.published_at,
                heading=chunk.heading,
                source_path=entry.path,
                source_url=source.source_url(entry.path, revision),
                source_revision=revision,
                source_blob_sha=entry.blob_sha,
                chunking_version=chunking_version,
                indexed_at=indexed_at,
                text=chunk.text,
                embedding=embedding,
            )
            for chunk, embedding in zip(content_chunks, embeddings, strict=True)
        )
        staged.append((article.slug, chunks))

    for article_id, chunks in staged:
        index.replace_article(article_id, chunks)
    for article_id in plan.delete:
        index.delete_article(article_id)
    return SyncResult(
        revision=revision,
        unchanged=not plan.reindex and not plan.delete,
        indexed=tuple(article_id for article_id, _ in staged),
        deleted=plan.delete,
        errors=tuple(errors),
    )
