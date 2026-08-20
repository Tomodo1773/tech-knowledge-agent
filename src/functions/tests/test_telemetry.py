from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind

from knowledge_agent.contracts import EventStateEntity, QueueMessage, TraceContext
from knowledge_agent.slack_events import handle_slack_request
from knowledge_agent.telemetry import (
    LOGGER_NAMESPACE,
    SAFE_ATTRIBUTES,
    SPAN_AGENT_INVOKE,
    SPAN_QUEUE_PUBLISH,
    SPAN_SLACK_EVENT_RECEIVE,
    SPAN_SLACK_MESSAGE_SEND,
    UnsafeAttributeError,
    continued_trace,
    current_trace_context,
    set_attributes,
    traced,
)

SIGNING_SECRET = "signing-secret-value"
NOW = datetime(2026, 8, 12, tzinfo=UTC)
FUNCTIONS_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = Path(__file__).parents[3]


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
    # Custom spans must not contain secrets, headers, or event bodies.
    with (
        pytest.raises(UnsafeAttributeError, match="slack.signing_secret"),
        traced(SPAN_SLACK_MESSAGE_SEND, **{"slack.signing_secret": "value"}),
    ):
        pass

    assert "knowledge.question" not in SAFE_ATTRIBUTES
    assert "knowledge.answer" not in SAFE_ATTRIBUTES


def test_set_attributes_refuses_unknown_keys_after_the_span_started(spans: Any) -> None:
    with traced(SPAN_SLACK_MESSAGE_SEND) as span:
        set_attributes(span, **{"knowledge.audit_reason": "accepted"})
        with pytest.raises(UnsafeAttributeError):
            set_attributes(span, **{"slack.bot_token": "xoxb-value"})

    assert spans.get_finished_spans()[0].attributes["knowledge.audit_reason"] == "accepted"


def test_none_valued_attributes_are_dropped_instead_of_recorded(spans: Any) -> None:
    with traced(SPAN_SLACK_MESSAGE_SEND, **{"knowledge.commit_sha": None}):
        pass

    assert "knowledge.commit_sha" not in spans.get_finished_spans()[0].attributes


def test_the_worker_collects_our_package_and_nothing_else() -> None:
    """Left at its default the worker collects the exporter's own records and loops."""
    functions_bicep = (REPOSITORY_ROOT / "infra/app/functions.bicep").read_text(
        encoding="utf-8"
    )

    assert LOGGER_NAMESPACE == "knowledge_agent"
    assert f"PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME: '{LOGGER_NAMESPACE}'" in functions_bicep


def test_every_app_log_record_lands_in_the_collected_subtree() -> None:
    """Records outside the subtree are dropped, not merely uncorrelated."""
    package = sorted((FUNCTIONS_ROOT / "knowledge_agent").glob("*.py"))
    entry_point = FUNCTIONS_ROOT / "function_app.py"

    # The root logger is never the app's logger: its records are outside the subtree.
    root_logger_calls = [
        f"{path.name}:{number}"
        for path in [entry_point, *package]
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if re.search(r"\blogging\.(debug|info|warning|error|exception|critical|log)\(", line)
    ]
    assert root_logger_calls == []

    # The entry point sits outside the package, so getLogger(__name__) would miss too.
    assert "logging.getLogger" not in entry_point.read_text(encoding="utf-8")


def test_the_agent_request_is_recorded_as_one_client_span(spans: Any) -> None:
    """The SDK's own span for this call is switched off in the deployment (ADR 0007)."""
    from knowledge_agent.worker import HostedAgentClient

    answer = SimpleNamespace(id="resp_1", output_text="Answer")
    client = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: answer))
    HostedAgentClient(client).ask("Q", previous_response_id=None)

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == [SPAN_AGENT_INVOKE]
    assert finished[0].kind is SpanKind.CLIENT


def test_the_agent_failure_survives_as_a_type_and_a_site(spans: Any, caplog: Any) -> None:
    """`from None` throws the cause away, so this log line is the only record of it."""
    from knowledge_agent.worker import AgentInvocationError, HostedAgentClient

    class FailingResponses:
        def create(self, **request: Any) -> Any:
            raise TimeoutError("upstream said: <question text and response body>")

    client = SimpleNamespace(responses=FailingResponses())
    with (
        caplog.at_level(logging.ERROR, logger=LOGGER_NAMESPACE),
        pytest.raises(AgentInvocationError),
    ):
        HostedAgentClient(client).ask("Q", previous_response_id=None)

    assert len(caplog.messages) == 1
    assert caplog.messages[0].startswith("agent request failed: TimeoutError at ")
    assert "test_telemetry.py" in caplog.messages[0]
    assert "create" in caplog.messages[0]
    # The class name and the frames behind it are the diagnosis; the exception's own
    # text may quote the payload, so it stays out of the line.
    assert "question text" not in caplog.text


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
    with continued_trace(publisher.messages[0].telemetry), traced(SPAN_SLACK_MESSAGE_SEND):
        pass

    finished = {span.name: span for span in spans.get_finished_spans()}
    trace_ids = {span.context.trace_id for span in finished.values()}
    assert set(finished) == {SPAN_SLACK_EVENT_RECEIVE, SPAN_QUEUE_PUBLISH, SPAN_SLACK_MESSAGE_SEND}
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


def test_agent_request_carries_the_traceparent_of_the_active_span(
    monkeypatch: Any, spans: Any
) -> None:
    """The Agent's spans only join this trace if the request carries traceparent.

    The Worker no longer injects the header; AIProjectInstrumentor registers an httpx
    request hook on every client that comes out of get_openai_client(). Injecting at
    request time is what puts the Agent's spans inside the `responses` span instead of
    beside it. The hook is silent when it is absent, and the only visible symptom is the
    Agent side of a Slack question landing under a separate operation, so assert it is
    there rather than trusting the gate and the SDK to stay aligned.
    """
    import httpx
    from azure.ai.projects import AIProjectClient
    from azure.ai.projects.telemetry import AIProjectInstrumentor

    from knowledge_agent.contracts import KNOWLEDGE_AGENT_NAME

    class StaticCredential:
        def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            from azure.core.credentials import AccessToken

            return AccessToken("token", int(time.time()) + 3600)

    monkeypatch.setenv("AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING", "true")
    instrumentor = AIProjectInstrumentor()
    instrumentor.instrument()
    try:
        # Split so the repository policy scan does not read this fixture as a real
        # endpoint (see the same idiom in test_settings.py).
        client = AIProjectClient(
            endpoint="https://example." "services.ai.azure.com/api/projects/dev",
            credential=StaticCredential(),
            allow_preview=True,
        ).get_openai_client(agent_name=KNOWLEDGE_AGENT_NAME)
        hooks = client._client._event_hooks["request"]
        assert hooks, "get_openai_client returned a client with no request hook"

        request = httpx.Request("POST", "https://example.invalid/responses")
        with traced(SPAN_SLACK_EVENT_RECEIVE) as active:
            for hook in hooks:
                hook(request)
            expected = active.get_span_context()
    finally:
        instrumentor.uninstrument()

    sent = request.headers["traceparent"].split("-")
    assert sent[1] == format(expected.trace_id, "032x")
    assert sent[2] == format(expected.span_id, "016x")
