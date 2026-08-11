from __future__ import annotations

from dataclasses import dataclass

import pytest

from azure_search import (
    CORPUS_ID,
    CosmosVectorSearchIndex,
    FoundryQueryEmbedder,
    SearchAdapterError,
)
from knowledge_search import EMBEDDING_DIMENSIONS

REVISION = "0123456789abcdef0123456789abcdef01234567"
SOURCE_URL = f"https://github.com/acme/blog/blob/{REVISION}/articles/azure.md"


@dataclass
class EmbeddingItem:
    embedding: tuple[float, ...]


class FakeEmbeddings:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        return type("Response", (), {"data": [EmbeddingItem((0.0,) * EMBEDDING_DIMENSIONS)]})()


def test_foundry_query_embedding_uses_dedicated_deployment_and_dimensions() -> None:
    embeddings = FakeEmbeddings()
    client = type("Client", (), {"embeddings": embeddings})()

    result = FoundryQueryEmbedder(client, "embedding-model").embed_query("azure")

    assert result == (0.0,) * EMBEDDING_DIMENSIONS
    assert embeddings.calls == [
        {
            "model": "embedding-model",
            "input": ["azure"],
            "dimensions": EMBEDDING_DIMENSIONS,
        }
    ]


def test_embedding_failure_is_sanitized() -> None:
    foundry_host = "secret." + "services.ai.azure.com"
    secret = f"https://{foundry_host}/api/projects/private"
    client = type("Client", (), {"embeddings": FakeEmbeddings(failure=RuntimeError(secret))})()

    with pytest.raises(SearchAdapterError) as captured:
        FoundryQueryEmbedder(client, "embedding-model").embed_query("secret query")

    assert secret not in str(captured.value)
    assert "secret query" not in str(captured.value)


class FakeContainer:
    def __init__(self, items: tuple[object, ...]) -> None:
        self.items = items
        self.calls: list[dict[str, object]] = []

    def query_items(self, **kwargs: object) -> tuple[object, ...]:
        self.calls.append(kwargs)
        return self.items


def test_cosmos_query_is_bounded_partitioned_and_distance_ordered() -> None:
    container = FakeContainer(
        (
            {
                "title": "Azure",
                "sourceUrl": SOURCE_URL,
                "text": "Excerpt",
                "distance": 0.09,
            },
        )
    )
    embedding = (0.0,) * EMBEDDING_DIMENSIONS

    results = CosmosVectorSearchIndex(container).search(embedding, 5)

    call = container.calls[0]
    assert "SELECT TOP @limit" in str(call["query"])
    assert "ORDER BY VectorDistance" in str(call["query"])
    assert call["partition_key"] == CORPUS_ID
    assert call["parameters"] == [
        {"name": "@limit", "value": 5},
        {"name": "@embedding", "value": list(embedding)},
        {"name": "@corpusId", "value": CORPUS_ID},
    ]
    assert results[0].distance == 0.09


@pytest.mark.parametrize("limit", [0, 21, True, 1.0])
def test_cosmos_query_rejects_unbounded_top(limit: object) -> None:
    with pytest.raises(ValueError, match="limit"):
        CosmosVectorSearchIndex(FakeContainer(())).search((0.0,) * EMBEDDING_DIMENSIONS, limit)  # type: ignore[arg-type]


def test_cosmos_rejects_untrusted_result_url() -> None:
    container = FakeContainer(
        ({"title": "Bad", "sourceUrl": "https://evil.test", "text": "x", "distance": 0.1},)
    )

    with pytest.raises(SearchAdapterError, match="result is invalid"):
        CosmosVectorSearchIndex(container).search((0.0,) * EMBEDDING_DIMENSIONS, 1)
