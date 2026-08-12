"""Fixed spans, W3C propagation, and an attribute allowlist for the Function App.

Span names are low-cardinality constants so they group in Application Insights.
Attribute keys are allowlisted because [quality.md](../../../docs/quality.md) forbids
recording Slack secrets, authorization headers, or event bodies on custom spans.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, format_span_id, format_trace_id
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from knowledge_agent.contracts import TraceContext

TRACER_NAME = "knowledge_agent"

SPAN_SYNC_RUN = "github.sync.run"
SPAN_TREE_FETCH = "github.tree.fetch"
SPAN_CONTENTS_FETCH = "github.contents.fetch"
SPAN_EMBEDDING_CREATE = "embedding.create"
SPAN_COSMOS_UPSERT = "cosmos.upsert"
SPAN_SLACK_EVENT_RECEIVE = "slack.event.receive"
SPAN_QUEUE_PUBLISH = "queue.publish"
SPAN_AGENT_INVOKE = "agent.invoke"
SPAN_SLACK_MESSAGE_SEND = "slack.message.send"

# Identifiers, counts, and outcomes only. Questions, answers, tokens, and Slack
# headers never become span attributes.
SAFE_ATTRIBUTES = frozenset(
    {
        "knowledge.article_count",
        "knowledge.audit_reason",
        "knowledge.chunk_count",
        "knowledge.commit_sha",
        "knowledge.conversation_continued",
        "knowledge.deleted_count",
        "knowledge.error_count",
        "knowledge.event_id",
        "knowledge.markdown_length",
        "knowledge.reindexed_count",
        "knowledge.response_id",
        "knowledge.run_result",
        "knowledge.thread_key",
    }
)

_PROPAGATOR = TraceContextTextMapPropagator()


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
    """Run a block inside one fixed span, recording allowlisted attributes only."""
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name, kind=kind, attributes=_validate(attributes)) as span:
        yield span


def set_attributes(span: trace.Span, **attributes: Any) -> None:
    """Add allowlisted attributes that are only known once the work has finished."""
    for key, value in _validate(attributes).items():
        span.set_attribute(key, value)


def trace_headers() -> dict[str, str]:
    """W3C headers for an outgoing HTTP call that no instrumentation covers.

    The OpenAI client talks over httpx, which the Azure Monitor distro does not
    auto-instrument, so nothing injects traceparent into the Responses request and the
    Hosted Agent starts a trace of its own. Foundry forwards this header into the
    container, so sending it explicitly is what joins the Agent's spans to the Slack
    request's trace. Empty when no span is active, which the client passes through
    unchanged.
    """
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier


def current_trace_context() -> TraceContext | None:
    """Return the active context in the shape the Queue message contract carries."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    traceparent = carrier.get("traceparent")
    if traceparent is None:
        return None
    try:
        return TraceContext(traceparent=traceparent, tracestate=carrier.get("tracestate"))
    except ValueError:
        return None


@contextmanager
def continued_trace(telemetry: TraceContext) -> Iterator[None]:
    """Attach the producer's trace so worker spans join the Slack request's trace."""
    token = otel_context.attach(_PROPAGATOR.extract(telemetry.to_dict()))
    try:
        yield
    finally:
        otel_context.detach(token)


def log_correlation() -> dict[str, str]:
    """Trace and span IDs for log lines, so logs and spans line up in queries."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": format_trace_id(span_context.trace_id),
        "span_id": format_span_id(span_context.span_id),
    }
