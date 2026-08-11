from __future__ import annotations

import hashlib
import hmac

from knowledge_agent.contracts import TraceContext
from knowledge_agent.slack_events import (
    AcceptedSlackEvent,
    IgnoredSlackEvent,
    SlackChallenge,
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
    assert message.correlation_id == "Ev1"
    assert accepted.event_state("2026-08-11T00:00:00Z").to_entity()["RowKey"] == "Ev1"


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
