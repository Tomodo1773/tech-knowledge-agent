from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from knowledge_agent.azure_adapters import (
    _COSMOS_BATCH_ENVELOPE_BYTES,
    _COSMOS_BATCH_PAYLOAD_LIMIT,
    AdapterContractError,
    CosmosIndexRepository,
    ExternalServiceError,
    FoundryEmbeddingProvider,
    TableSyncStateStore,
    _partition_batch_operations,
    _serialized_operation_size,
)
from knowledge_agent.contracts import CORPUS_ID, EMBEDDING_DIMENSIONS, CosmosChunk, SyncStateEntity

REVISION = "0123456789abcdef0123456789abcdef01234567"
BLOB_SHA = "a" * 40
VECTOR = (0.0,) * EMBEDDING_DIMENSIONS


class MissingEntity(Exception):
    pass


class FakeTable:
    def __init__(self, entity: dict[str, str] | None = None) -> None:
        self.entity = entity
        self.upserts: list[tuple[dict[str, str], str]] = []

    def get_entity(self, **kwargs: str) -> dict[str, str]:
        assert kwargs == {"partition_key": "sync", "row_key": "github"}
        if self.entity is None:
            raise MissingEntity
        return self.entity

    def upsert_entity(self, *, entity: dict[str, str], mode: str) -> None:
        self.upserts.append((entity, mode))


def test_table_state_uses_contract_and_replace_upsert() -> None:
    table = FakeTable(
        {
            "PartitionKey": "sync",
            "RowKey": "github",
            "lastSuccessfulSha": REVISION,
            "lastRunAt": "2026-08-11T00:00:00Z",
            "lastRunResult": "success",
        }
    )
    store = TableSyncStateStore(table, not_found_error=MissingEntity)

    assert store.get().last_successful_sha == REVISION  # type: ignore[union-attr]
    state = SyncStateEntity(REVISION, "2026-08-11T01:00:00Z", "partial")
    store.put(state)

    assert table.upserts == [(state.to_entity(), "replace")]
    assert TableSyncStateStore(FakeTable(), not_found_error=MissingEntity).get() is None


def test_azure_sdk_failures_are_sanitized() -> None:
    class FailingTable(FakeTable):
        def get_entity(self, **kwargs: str) -> dict[str, str]:
            raise RuntimeError("https://secret." "table.core.windows.net credential")

    with pytest.raises(ExternalServiceError, match="Table state read failed") as captured:
        TableSyncStateStore(FailingTable(), not_found_error=MissingEntity).get()
    assert "secret" not in str(captured.value)

def _manifest_chunk(
    index: int,
    *,
    blob_sha: str = BLOB_SHA,
    version: str = "v1",
    item_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": item_id or f"article:{index}",
        "corpusId": CORPUS_ID,
        "articleId": "article",
        "chunkIndex": index,
        "sourcePath": "articles/article.md",
        "sourceRevision": REVISION,
        "sourceBlobSha": blob_sha,
        "chunkingVersion": version,
    }


class FakeContainer:
    def __init__(self, manifests: list[dict[str, object]]) -> None:
        self.manifests = manifests
        self.ids: list[dict[str, str]] = []
        self.batches: list[tuple[list[tuple[object, ...]], str]] = []

    def query_items(self, *, query: str, **kwargs: object) -> list[dict[str, object]]:
        if "SELECT c.id FROM" in query:
            return list(self.ids)
        assert kwargs["partition_key"] == CORPUS_ID
        return self.manifests

    def execute_item_batch(
        self,
        *,
        batch_operations: list[tuple[object, ...]],
        partition_key: str,
    ) -> None:
        self.batches.append((batch_operations, partition_key))


def test_cosmos_sdk_failures_are_sanitized() -> None:
    class FailingContainer(FakeContainer):
        def query_items(self, *, query: str, **kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("https://secret." "documents.azure.com credential")

    with pytest.raises(ExternalServiceError, match="Cosmos manifest read failed") as captured:
        CosmosIndexRepository(FailingContainer([])).list_articles()
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "manifests",
    [
        [_manifest_chunk(1)],
        [_manifest_chunk(0), _manifest_chunk(2)],
        [_manifest_chunk(0), _manifest_chunk(1, blob_sha="b" * 40)],
        [_manifest_chunk(0), _manifest_chunk(1, version="v2")],
        [_manifest_chunk(0, item_id="wrong")],
    ],
)
def test_manifest_inconsistency_marks_article_for_complete_reindex(
    manifests: list[dict[str, object]],
) -> None:
    article = CosmosIndexRepository(FakeContainer(manifests)).list_articles()[0]

    assert article.article_id == "article"
    assert article.needs_reindex is True


def test_complete_manifest_is_reusable() -> None:
    article = CosmosIndexRepository(
        FakeContainer([_manifest_chunk(0), _manifest_chunk(1)])
    ).list_articles()[0]

    assert article.needs_reindex is False
    assert article.source_blob_sha == BLOB_SHA


def _chunk(index: int, *, blob_sha: str = BLOB_SHA) -> CosmosChunk:
    return CosmosChunk(
        id=f"article:{index}",
        corpus_id=CORPUS_ID,
        article_id="article",
        chunk_index=index,
        slug="article",
        title="Article",
        emoji="test",
        article_type="tech",
        topics=("azure",),
        published=True,
        published_at=None,
        heading=None,
        source_path="articles/article.md",
        source_url=f"https://github.com/example/blog/blob/{REVISION}/articles/article.md",
        source_revision=REVISION,
        source_blob_sha=blob_sha,
        chunking_version="v1",
        indexed_at="2026-08-11T00:00:00Z",
        text=f"chunk {index}",
        embedding=VECTOR,
    )


def test_small_reduction_is_one_atomic_upsert_and_delete_batch() -> None:
    container = FakeContainer([])
    container.ids = [{"id": f"article:{index}"} for index in range(3)]

    CosmosIndexRepository(container).replace_article(
        "article",
        tuple(_chunk(index) for index in range(2)),
    )

    assert len(container.batches) == 1
    operations, partition = container.batches[0]
    assert partition == CORPUS_ID
    assert [operation[0] for operation in operations] == ["upsert", "upsert", "delete"]
    assert operations[0][1][0]["chunkIndex"] == 1  # type: ignore[index]
    assert operations[1][1][0]["chunkIndex"] == 0  # type: ignore[index]
    assert operations[2] == ("delete", ("article:2",))


def test_exact_hundred_operation_boundary_stays_in_one_batch() -> None:
    operations = tuple(("delete", (f"article:{index}",)) for index in range(100))

    batches = _partition_batch_operations(operations)

    assert len(batches) == 1
    assert len(batches[0]) == 100


def test_payload_boundary_is_inclusive_and_overflow_starts_next_batch() -> None:
    empty_operation = ("delete", ("",))
    empty_size = _serialized_operation_size(empty_operation)
    identifier_size = _COSMOS_BATCH_PAYLOAD_LIMIT - _COSMOS_BATCH_ENVELOPE_BYTES - empty_size
    boundary_operation = ("delete", ("x" * identifier_size,))
    assert (
        _COSMOS_BATCH_ENVELOPE_BYTES + _serialized_operation_size(boundary_operation)
        == _COSMOS_BATCH_PAYLOAD_LIMIT
    )

    batches = _partition_batch_operations((boundary_operation, ("delete", ("next",))))

    assert [len(batch) for batch in batches] == [1, 1]


def test_single_operation_over_safe_payload_limit_is_rejected() -> None:
    oversized = ("delete", ("x" * _COSMOS_BATCH_PAYLOAD_LIMIT,))

    with pytest.raises(AdapterContractError, match="safe size limit"):
        _partition_batch_operations((oversized,))


def test_large_replace_upserts_high_indices_then_stale_deletes() -> None:
    container = FakeContainer([])
    container.ids = [{"id": "article:stale"}]
    repository = CosmosIndexRepository(container)

    repository.replace_article("article", tuple(_chunk(index) for index in range(101)))

    assert len(container.batches) == 2
    first_operations, partition = container.batches[0]
    assert partition == CORPUS_ID
    assert first_operations[0][0] == "upsert"
    assert first_operations[0][1][0]["chunkIndex"] == 100  # type: ignore[index]
    assert first_operations[-1][1][0]["chunkIndex"] == 1  # type: ignore[index]
    assert container.batches[1][0][0][1][0]["chunkIndex"] == 0  # type: ignore[index]
    assert container.batches[1][0][1] == (
        "delete",
        ("article:stale",),
    )


def test_interrupted_large_replace_is_detected_for_next_full_reindex() -> None:
    class InterruptingContainer(FakeContainer):
        def __init__(self) -> None:
            super().__init__([_manifest_chunk(index, blob_sha=BLOB_SHA) for index in range(3)])

        def query_items(self, *, query: str, **kwargs: object) -> list[dict[str, object]]:
            if "SELECT c.id FROM" in query:
                return [{"id": item_id} for item_id in sorted(self._documents)]
            return list(self._documents.values())

        def execute_item_batch(
            self,
            *,
            batch_operations: list[tuple[object, ...]],
            partition_key: str,
        ) -> None:
            if self.batches:
                raise RuntimeError("simulated second-batch failure")
            self.batches.append((batch_operations, partition_key))
            for operation, arguments in batch_operations:
                assert operation == "upsert"
                document = arguments[0]
                self._documents[document["id"]] = document  # type: ignore[index]

        @property
        def _documents(self) -> dict[str, dict[str, object]]:
            if not hasattr(self, "documents"):
                self.documents = {item["id"]: item for item in self.manifests}  # type: ignore[misc]
            return self.documents

    container = InterruptingContainer()
    repository = CosmosIndexRepository(container)

    with pytest.raises(ExternalServiceError, match="Cosmos article write failed"):
        repository.replace_article(
            "article",
            tuple(_chunk(index, blob_sha="b" * 40) for index in range(101)),
        )

    manifest = repository.list_articles()[0]
    assert len(container.batches[0][0]) == 100
    assert manifest.needs_reindex is True


@dataclass
class FakeEmbeddings:
    data: list[SimpleNamespace]

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(data=self.data)


def test_foundry_embedding_restores_response_order_and_validates_vectors() -> None:
    embeddings = FakeEmbeddings(
        [
            SimpleNamespace(index=1, embedding=[1.0] * EMBEDDING_DIMENSIONS),
            SimpleNamespace(index=0, embedding=[0.0] * EMBEDDING_DIMENSIONS),
        ]
    )
    provider = FoundryEmbeddingProvider(
        SimpleNamespace(embeddings=embeddings),
        "text-embedding-3-small",
    )

    result = provider.embed(("first", "second"))

    assert result[0][0] == 0.0
    assert result[1][0] == 1.0
    assert embeddings.kwargs == {
        "model": "text-embedding-3-small",
        "input": ["first", "second"],
        "dimensions": EMBEDDING_DIMENSIONS,
    }


def test_foundry_embedding_rejects_partial_or_non_finite_response() -> None:
    provider = FoundryEmbeddingProvider(
        SimpleNamespace(
            embeddings=FakeEmbeddings(
                [SimpleNamespace(index=0, embedding=[float("nan")] * EMBEDDING_DIMENSIONS)]
            )
        ),
        "deployment",
    )
    with pytest.raises(AdapterContractError, match="vector"):
        provider.embed(("text",))

    partial = FoundryEmbeddingProvider(
        SimpleNamespace(embeddings=FakeEmbeddings([])),
        "deployment",
    )
    with pytest.raises(AdapterContractError, match="count"):
        partial.embed(("text",))
