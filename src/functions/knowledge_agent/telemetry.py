"""The Function App's two telemetry channels, per [telemetry.md](../../../docs/telemetry.md).

Spans are the primary channel: fixed low-cardinality names so they group in Application
Insights, an allowlist of attribute keys, and W3C propagation across the Queue.

The log channel carries only what a span cannot, which today means the cause of a failure
whose exception the app deliberately replaces. A log message has no allowlist, so the
content rules apply by hand: identifiers, outcomes, and exception types only.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind
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
# Not "agent.invoke": the platform's own server span is named invoke_agent, and the two
# reversed word orders side by side in a trace read as duplicates of one another.
SPAN_AGENT_REQUEST = "agent.request"
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

# The Python worker passes the app setting PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME to
# configure_azure_monitor as logger_name and collects that one subtree. This package is
# that subtree, so `logging.getLogger(__name__)` from any module here is collected and
# anything else is not. The setting's default is the root logger, which collects the
# exporter's own records and makes delivering telemetry produce telemetry.
LOGGER_NAMESPACE = __name__.split(".")[0]


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


# Nothing stamps trace and span IDs onto log lines by hand. The handler the distro
# attaches builds every record with `context=get_current()`, so a record emitted inside a
# span is already correlated; stamping the IDs again would only repeat them in the body.
