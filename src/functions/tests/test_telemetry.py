from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from knowledge_agent.contracts import EventStateEntity, QueueMessage, TraceContext
from knowledge_agent.slack_events import handle_slack_request
from knowledge_agent.telemetry import (
    SAFE_ATTRIBUTES,
    SPAN_AGENT_INVOKE,
    SPAN_QUEUE_PUBLISH,
    SPAN_SLACK_EVENT_RECEIVE,
    UnsafeAttributeError,
    continued_trace,
    current_trace_context,
    log_correlation,
    set_attributes,
    traced,
)

SIGNING_SECRET = "signing-secret-value"
NOW = datetime(2026, 8, 12, tzinfo=UTC)


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


def test_span_attributes_outside_the_allowlist_are_refused() -> None:
    # quality.md forbids secrets, headers, and event bodies on custom spans.
    with (
        pytest.raises(UnsafeAttributeError, match="slack.signing_secret"),
        traced(SPAN_AGENT_INVOKE, **{"slack.signing_secret": "value"}),
    ):
        pass

    assert "knowledge.question" not in SAFE_ATTRIBUTES
    assert "knowledge.answer" not in SAFE_ATTRIBUTES


def test_set_attributes_refuses_unknown_keys_after_the_span_started(spans: Any) -> None:
    with traced(SPAN_AGENT_INVOKE) as span:
        set_attributes(span, **{"knowledge.response_id": "resp_1"})
        with pytest.raises(UnsafeAttributeError):
            set_attributes(span, **{"slack.bot_token": "xoxb-value"})

    assert spans.get_finished_spans()[0].attributes["knowledge.response_id"] == "resp_1"


def test_none_valued_attributes_are_dropped_instead_of_recorded(spans: Any) -> None:
    with traced(SPAN_AGENT_INVOKE, **{"knowledge.commit_sha": None}):
        pass

    assert "knowledge.commit_sha" not in spans.get_finished_spans()[0].attributes


def test_log_correlation_exposes_ids_only_inside_a_span(spans: Any) -> None:
    assert log_correlation() == {}
    with traced(SPAN_AGENT_INVOKE):
        correlation = log_correlation()
    assert len(correlation["trace_id"]) == 32
    assert len(correlation["span_id"]) == 16


class FakeEventStore:
    def claim(self, event: EventStateEntity) -> bool:
        return True


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[QueueMessage] = []

    def publish(self, message: QueueMessage) -> None:
        with traced(SPAN_QUEUE_PUBLISH, **{"knowledge.event_id": message.event_id}):
            self.messages.append(message)


def _signed_request() -> dict[str, Any]:
    payload = {
        "type": "event_callback",
        "event_id": "Ev1",
        "team_id": "T1",
        "event": {
            "type": "message",
            "channel_type": "im",
            "user": "U1",
            "channel": "D1",
            "ts": "1720000000.000001",
            "text": "Question",
        },
    }
    body = json.dumps(payload).encode()
    timestamp = int(NOW.timestamp())
    digest = hmac.new(
        SIGNING_SECRET.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        "raw_body": body,
        "timestamp_header": str(timestamp),
        "signature_header": f"v0={digest}",
    }


def test_one_slack_question_stays_one_trace_across_the_queue(spans: Any) -> None:
    publisher = FakePublisher()
    with traced(SPAN_SLACK_EVENT_RECEIVE):
        handle_slack_request(
            **_signed_request(),
            signing_secret=SIGNING_SECRET,
            allowed_team_id="T1",
            allowed_user_id="U1",
            event_store=FakeEventStore(),
            publisher=publisher,
            now=lambda: NOW,
            trace_context=lambda: current_trace_context() or TraceContext(
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
            ),
        )

    # The worker rejoins from the queue message alone, in a separate process.
    with continued_trace(publisher.messages[0].telemetry), traced(SPAN_AGENT_INVOKE):
        pass

    finished = {span.name: span for span in spans.get_finished_spans()}
    trace_ids = {span.context.trace_id for span in finished.values()}
    assert set(finished) == {SPAN_SLACK_EVENT_RECEIVE, SPAN_QUEUE_PUBLISH, SPAN_AGENT_INVOKE}
    assert len(trace_ids) == 1
    assert finished[SPAN_QUEUE_PUBLISH].attributes["knowledge.event_id"] == "Ev1"


def test_queue_message_telemetry_carries_the_publishing_span(spans: Any) -> None:
    with traced(SPAN_SLACK_EVENT_RECEIVE) as span:
        context = current_trace_context()

    assert context is not None
    expected = format(span.get_span_context().trace_id, "032x")
    assert context.traceparent.split("-")[1] == expected


def test_no_active_span_yields_no_trace_context() -> None:
    assert current_trace_context() is None
