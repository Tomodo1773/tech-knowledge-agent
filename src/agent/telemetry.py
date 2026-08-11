"""Custom spans for the Hosted Agent's search tool.

The Responses protocol runtime instruments the agent turn itself. These spans cover
the tool work it cannot see. Built-in `gen_ai` telemetry may capture message content,
but custom span attributes must stay limited to counts and scores, so the attribute
keys here are allowlisted the same way the Function App does it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import SpanKind

TRACER_NAME = "knowledge_agent"

SPAN_KNOWLEDGE_SEARCH = "knowledge.search"
SPAN_COSMOS_VECTOR_QUERY = "cosmos.vector_query"

# Never the query text, the article text, or the answer.
SAFE_ATTRIBUTES = frozenset(
    {
        "knowledge.limit",
        "knowledge.min_distance",
        "knowledge.result_count",
    }
)


class UnsafeAttributeError(ValueError):
    """Raised when a caller tries to record an attribute outside the allowlist."""


def _validate(attributes: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(attributes) - SAFE_ATTRIBUTES)
    if unknown:
        raise UnsafeAttributeError(f"attributes are not allowlisted: {unknown}")
    return {key: value for key, value in attributes.items() if value is not None}


@contextmanager
def traced(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    **attributes: Any,
) -> Iterator[trace.Span]:
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, kind=kind, attributes=_validate(attributes)) as span:
        yield span


def set_attributes(span: trace.Span, **attributes: Any) -> None:
    for key, value in _validate(attributes).items():
        span.set_attribute(key, value)
