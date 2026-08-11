from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from knowledge_agent.contracts import EventStateEntity, QueueMessage, TraceContext
from knowledge_agent.slack_events import (
    AcceptedSlackEvent,
    IgnoredSlackEvent,
    SlackApiError,
    SlackChallenge,
    SlackWebClient,
    handle_slack_request,
    new_trace_context,
    select_slack_event,
    truncate_slack_markdown,
    verify_slack_signature,
)


def _signature(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"v0={digest}"


def test_verifies_signature_and_rejects_stale_or_modified_request() -> None:
    body = b'{"type":"event_callback"}'
    timestamp = 1_720_000_000
    signature = _signature("secret", timestamp, body)

    assert verify_slack_signature(body, timestamp, signature, "secret", now=timestamp + 299)
    assert verify_slack_signature(body, timestamp, signature, "secret", now=timestamp + 300)
    assert not verify_slack_signature(body + b" ", timestamp, signature, "secret", now=timestamp)
    assert not verify_slack_signature(body, timestamp, signature, "secret", now=timestamp + 301)
    assert not verify_slack_signature(body, timestamp, signature, "secret", now=timestamp - 301)


def test_selects_challenge_and_allowed_dm_into_contracts() -> None:
    challenge = select_slack_event(
        {"type": "url_verification", "challenge": "challenge-value"},
        allowed_team_id="T1",
        allowed_user_id="U1",
    )
    assert challenge == SlackChallenge("challenge-value")

    accepted = select_slack_event(
        {
            "type": "event_callback",
            "event_id": "Ev1",
            "team_id": "T1",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": "U1",
                "channel": "D1",
                "ts": "1720000000.000001",
                "thread_ts": "1719999999.000001",
                "text": "Question",
            },
        },
        allowed_team_id="T1",
        allowed_user_id="U1",
    )
    assert isinstance(accepted, AcceptedSlackEvent)
    message = accepted.queue_message(
        TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    )
    assert message.root_ts == "1719999999.000001"
    # The thread parent identifies where to reply; the event ts identifies what to react to.
    assert message.message_ts == "1720000000.000001"
    assert message.correlation_id == "Ev1"
    assert accepted.event_state("2026-08-11T00:00:00Z").to_entity()["RowKey"] == "Ev1"


def test_top_level_dm_uses_the_same_timestamp_for_thread_and_reaction() -> None:
    accepted = select_slack_event(
        {
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
        },
        allowed_team_id="T1",
        allowed_user_id="U1",
    )
    assert isinstance(accepted, AcceptedSlackEvent)
    assert accepted.root_ts == accepted.message_ts == "1720000000.000001"


def test_ignores_untrusted_or_bot_messages_without_queue_payload() -> None:
    base = {
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
            "bot_id": "B1",
        },
    }
    assert isinstance(
        select_slack_event(base, allowed_team_id="T1", allowed_user_id="U1"),
        IgnoredSlackEvent,
    )
    assert isinstance(
        select_slack_event(base, allowed_team_id="OTHER", allowed_user_id="U1"),
        IgnoredSlackEvent,
    )


def test_truncates_body_before_sources_and_preserves_all_urls() -> None:
    markdown = (
        "Long answer " * 50
        + "\n\n## Sources\n"
        + "- [A](https://github.com/acme/blog/blob/revision/articles/a.md)\n"
        + "- [B](https://github.com/acme/blog/blob/revision/articles/b.md)"
    )

    truncated = truncate_slack_markdown(markdown, limit=240)

    assert len(truncated) <= 240
    assert truncated.endswith("articles/b.md)")
    assert "articles/a.md" in truncated
    assert truncated.count("## Sources") == 1


class FakeSlackTransport:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, payload))
        return self.responses.pop(0)


def test_web_client_replies_as_standard_markdown_without_unfurling() -> None:
    transport = FakeSlackTransport({"ok": True}, {"ok": True})
    client = SlackWebClient(transport)

    client.add_eyes_reaction(channel_id="D1", timestamp="1720000000.000001")
    client.post_thread_reply(
        channel_id="D1",
        thread_ts="1720000000.000001",
        markdown="**Answer**\n\n## Sources\n- [A](https://example.test/a)",
    )

    assert transport.calls[0] == (
        "reactions.add",
        {"channel": "D1", "timestamp": "1720000000.000001", "name": "eyes"},
    )
    method, payload = transport.calls[1]
    assert method == "chat.postMessage"
    assert payload["markdown_text"].startswith("**Answer**")
    assert "text" not in payload and "blocks" not in payload
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False


def test_web_client_surfaces_slack_error_codes_only() -> None:
    client = SlackWebClient(FakeSlackTransport({"ok": False, "error": "not_in_channel"}))
    with pytest.raises(SlackApiError, match="not_in_channel"):
        client.add_eyes_reaction(channel_id="D1", timestamp="1.1")


class FakeEventStore:
    def __init__(self, claimed: bool = True) -> None:
        self.claimed = claimed
        self.events: list[EventStateEntity] = []

    def claim(self, event: EventStateEntity) -> bool:
        self.events.append(event)
        return self.claimed


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[QueueMessage] = []

    def publish(self, message: QueueMessage) -> None:
        self.messages.append(message)


SIGNING_SECRET = "signing-secret-value"
NOW = datetime(2026, 8, 11, tzinfo=UTC)
DM_PAYLOAD = {
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


def _request(payload: dict[str, Any] | bytes, *, secret: str = SIGNING_SECRET):  # type: ignore[no-untyped-def]
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    timestamp = int(NOW.timestamp())
    return {
        "raw_body": body,
        "timestamp_header": str(timestamp),
        "signature_header": _signature(secret, timestamp, body),
    }


def _handle(payload: dict[str, Any] | bytes, store: FakeEventStore, publisher: FakePublisher):  # type: ignore[no-untyped-def]
    return handle_slack_request(
        **_request(payload),
        signing_secret=SIGNING_SECRET,
        allowed_team_id="T1",
        allowed_user_id="U1",
        event_store=store,
        publisher=publisher,
        now=lambda: NOW,
        trace_context=lambda: TraceContext(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        ),
    )


def test_accepted_dm_is_claimed_once_and_enqueued() -> None:
    store, publisher = FakeEventStore(), FakePublisher()
    result = _handle(DM_PAYLOAD, store, publisher)

    assert (result.status_code, result.audit_reason) == (200, "accepted")
    assert store.events[0].to_entity()["receivedAt"] == "2026-08-11T00:00:00Z"
    assert publisher.messages[0].question == "Question"


def test_slack_redelivery_is_answered_without_a_second_queue_message() -> None:
    store, publisher = FakeEventStore(claimed=False), FakePublisher()
    result = _handle(DM_PAYLOAD, store, publisher)

    assert (result.status_code, result.audit_reason) == (200, "duplicate_event")
    assert publisher.messages == []


def test_url_verification_answers_the_challenge_without_enqueueing() -> None:
    store, publisher = FakeEventStore(), FakePublisher()
    result = _handle(
        {"type": "url_verification", "challenge": "challenge-value"}, store, publisher
    )

    assert (result.status_code, result.body) == (200, "challenge-value")
    assert (store.events, publisher.messages) == ([], [])


def test_untrusted_sources_are_audited_with_the_documented_vocabulary() -> None:
    store, publisher = FakeEventStore(), FakePublisher()

    foreign_user = dict(DM_PAYLOAD, event=dict(DM_PAYLOAD["event"], user="U_OTHER"))  # type: ignore[arg-type]
    channel = dict(DM_PAYLOAD, event=dict(DM_PAYLOAD["event"], channel_type="channel"))  # type: ignore[arg-type]
    foreign_team = dict(DM_PAYLOAD, team_id="T_OTHER")

    outcomes = [
        _handle(payload, store, publisher)
        for payload in (foreign_user, channel, foreign_team)
    ]

    # Slack must not retry any of these, so every one is a 2xx with an audit record only.
    assert [result.status_code for result in outcomes] == [200, 200, 200]
    assert [result.audit_reason for result in outcomes] == [
        "unauthorized_source",
        "unsupported_conversation_type",
        "unauthorized_source",
    ]
    assert (store.events, publisher.messages) == ([], [])


def test_invalid_signature_or_body_never_reaches_storage() -> None:
    store, publisher = FakeEventStore(), FakePublisher()

    body = json.dumps(DM_PAYLOAD).encode()
    timestamp = int(NOW.timestamp())
    forged = handle_slack_request(
        raw_body=body,
        timestamp_header=str(timestamp),
        signature_header=_signature("wrong-secret", timestamp, body),
        signing_secret=SIGNING_SECRET,
        allowed_team_id="T1",
        allowed_user_id="U1",
        event_store=store,
        publisher=publisher,
        now=lambda: NOW,
    )
    assert (forged.status_code, forged.audit_reason) == (401, "invalid_signature")

    missing = handle_slack_request(
        raw_body=body,
        timestamp_header=None,
        signature_header=None,
        signing_secret=SIGNING_SECRET,
        allowed_team_id="T1",
        allowed_user_id="U1",
        event_store=store,
        publisher=publisher,
        now=lambda: NOW,
    )
    assert missing.status_code == 401

    broken = _handle(b"{not json", store, publisher)
    assert (broken.status_code, broken.audit_reason) == (400, "invalid_json")
    assert (store.events, publisher.messages) == ([], [])


def test_generated_trace_context_is_a_sampled_w3c_parent() -> None:
    first, second = new_trace_context(), new_trace_context()
    assert first.traceparent.startswith("00-")
    assert first.traceparent.endswith("-01")
    assert first.tracestate is None
    assert first.traceparent != second.traceparent
