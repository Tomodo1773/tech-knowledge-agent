"""Managed Identity adapters for query embedding and Cosmos vector search."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.trace import SpanKind

from knowledge_search import EMBEDDING_DIMENSIONS, SearchHit
from telemetry import SPAN_COSMOS_VECTOR_QUERY, set_attributes, traced

COSMOS_DATABASE_NAME = "knowledge"
COSMOS_CONTAINER_NAME = "chunks"
CORPUS_ID = "default"


class SearchAdapterError(RuntimeError):
    """Sanitized external-service or response-contract failure."""


def _required_text(item: Mapping[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SearchAdapterError("Cosmos vector result is invalid")
    return value


class FoundryQueryEmbedder:
    def __init__(self, openai_client: Any, deployment_name: str) -> None:
        if not isinstance(deployment_name, str) or not deployment_name.strip():
            raise ValueError("embedding deployment name must not be empty")
        self._client = openai_client
        self._deployment_name = deployment_name

    def embed_query(self, query: str) -> tuple[float, ...]:
        try:
            response = self._client.embeddings.create(
                model=self._deployment_name,
                input=[query],
                dimensions=EMBEDDING_DIMENSIONS,
            )
        except Exception:
            raise SearchAdapterError("Foundry query embedding failed") from None
        data = getattr(response, "data", None)
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)) or len(data) != 1:
            raise SearchAdapterError("Foundry query embedding response is invalid")
        values = getattr(data[0], "embedding", None)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise SearchAdapterError("Foundry query embedding response is invalid")
        embedding = tuple(values)
        if len(embedding) != EMBEDDING_DIMENSIONS or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in embedding
        ):
            raise SearchAdapterError("Foundry query embedding response is invalid")
        return embedding


class CosmosVectorSearchIndex:
    def __init__(self, container_client: Any) -> None:
        self._container = container_client

    def search(self, embedding: tuple[float, ...], limit: int) -> tuple[SearchHit, ...]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        with traced(
            SPAN_COSMOS_VECTOR_QUERY,
            kind=SpanKind.CLIENT,
            **{"knowledge.limit": limit},
        ) as span:
            results = self._query(embedding, limit)
            set_attributes(span, **{"knowledge.result_count": len(results)})
            return results

    def _query(self, embedding: tuple[float, ...], limit: int) -> tuple[SearchHit, ...]:
        query = (
            "SELECT TOP @limit c.title, c.sourceUrl, c.text, "
            "VectorDistance(c.embedding, @embedding) AS distance FROM c "
            "WHERE c.corpusId = @corpusId "
            "ORDER BY VectorDistance(c.embedding, @embedding)"
        )
        try:
            items = tuple(
                self._container.query_items(
                    query=query,
                    parameters=[
                        {"name": "@limit", "value": limit},
                        {"name": "@embedding", "value": list(embedding)},
                        {"name": "@corpusId", "value": CORPUS_ID},
                    ],
                    partition_key=CORPUS_ID,
                )
            )
        except Exception:
            raise SearchAdapterError("Cosmos vector query failed") from None
        results: list[SearchHit] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise SearchAdapterError("Cosmos vector result is invalid")
            distance = item.get("distance")
            try:
                results.append(
                    SearchHit(
                        title=_required_text(item, "title"),
                        source_url=_required_text(item, "sourceUrl"),
                        text=_required_text(item, "text"),
                        distance=distance,
                    )
                )
            except (TypeError, ValueError):
                raise SearchAdapterError("Cosmos vector result is invalid") from None
        return tuple(results)
