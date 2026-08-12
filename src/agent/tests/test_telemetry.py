from __future__ import annotations

from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from azure_search import CosmosVectorSearchIndex
from knowledge_search import KnowledgeSearchService, SearchHit
from telemetry import (
    SAFE_ATTRIBUTES,
    SPAN_COSMOS_VECTOR_QUERY,
    SPAN_KNOWLEDGE_SEARCH,
    UnsafeAttributeError,
    traced,
)

EMBEDDING = tuple(0.0 for _ in range(1536))


@pytest.fixture
def spans() -> Any:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Save the module global, not get_tracer_provider(): restoring the proxy provider
    # into the global makes it delegate to itself and recurse forever.
    previous = trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]


class FakeEmbedder:
    def embed_query(self, query: str) -> tuple[float, ...]:
        return EMBEDDING


class FakeIndex:
    def __init__(self, *hits: SearchHit) -> None:
        self.hits = hits

    def search(self, embedding: tuple[float, ...], limit: int) -> tuple[SearchHit, ...]:
        return self.hits


def _hit(distance: float) -> SearchHit:
    return SearchHit(
        title="Article",
        source_url=(
            "https://github.com/acme/blog/blob/"
            "0123456789abcdef0123456789abcdef01234567/articles/a.md"
        ),
        text="Body",
        distance=distance,
    )


class FakeContainer:
    def query_items(self, **_: Any) -> tuple[dict[str, Any], ...]:
        return (
            {
                "title": "Article",
                "sourceUrl": (
                    "https://github.com/acme/blog/blob/"
                    "0123456789abcdef0123456789abcdef01234567/articles/a.md"
                ),
                "text": "Body",
                "distance": 0.25,
            },
        )


def test_query_and_answer_text_are_never_recorded_as_attributes() -> None:
    assert "knowledge.query" not in SAFE_ATTRIBUTES
    assert "knowledge.text" not in SAFE_ATTRIBUTES
    with (
        pytest.raises(UnsafeAttributeError, match="knowledge.query"),
        traced(SPAN_KNOWLEDGE_SEARCH, **{"knowledge.query": "secret question"}),
    ):
        pass


def test_search_records_result_count_and_best_distance(spans: Any) -> None:
    service = KnowledgeSearchService(FakeEmbedder(), FakeIndex(_hit(0.4), _hit(0.1)))

    service.search("How does the sync work?", limit=5)

    span = spans.get_finished_spans()[0]
    assert span.name == SPAN_KNOWLEDGE_SEARCH
    assert span.attributes["knowledge.limit"] == 5
    assert span.attributes["knowledge.result_count"] == 2
    assert span.attributes["knowledge.min_distance"] == 0.1


def test_search_without_evidence_omits_the_distance_attribute(spans: Any) -> None:
    KnowledgeSearchService(FakeEmbedder(), FakeIndex()).search("Question")

    span = spans.get_finished_spans()[0]
    assert span.attributes["knowledge.result_count"] == 0
    assert "knowledge.min_distance" not in span.attributes


def test_cosmos_vector_query_is_its_own_client_span(spans: Any) -> None:
    CosmosVectorSearchIndex(FakeContainer()).search(EMBEDDING, 3)

    span = spans.get_finished_spans()[0]
    assert span.name == SPAN_COSMOS_VECTOR_QUERY
    assert span.kind is trace.SpanKind.CLIENT
    assert span.attributes["knowledge.result_count"] == 1
