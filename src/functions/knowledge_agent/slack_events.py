"""Slack request validation, event selection, reply formatting, and Web API calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from opentelemetry.trace import SpanKind

from knowledge_agent.contracts import EventStateEntity, QueueMessage, TraceContext
from knowledge_agent.telemetry import SPAN_SLACK_MESSAGE_SEND, traced

SLACK_SIGNATURE_MAX_AGE_SECONDS = 300
SLACK_MARKDOWN_LIMIT = 4000
_SOURCES_SEPARATOR = "\n\n## Sources\n"

# Keep the audit vocabulary stable instead of exposing the selector's internals.
_AUDIT_REASONS = {
    "team_not_allowed": "unauthorized_source",
    "user_not_allowed": "unauthorized_source",
    "not_dm_message": "unsupported_conversation_type",
}


class SlackMessageFormatError(ValueError):
    """Raised when a Slack response cannot preserve its source contract."""


class SlackApiError(RuntimeError):
    """Raised with Slack's error code only, never with the bot token."""


@dataclass(frozen=True, slots=True)
class SlackChallenge:
    challenge: str


@dataclass(frozen=True, slots=True)
class IgnoredSlackEvent:
    reason: str


@dataclass(frozen=True, slots=True)
class AcceptedSlackEvent:
    event_id: str
    team_id: str
    user_id: str
    channel_id: str
    root_ts: str
    message_ts: str
    question: str

    def queue_message(self, telemetry: TraceContext) -> QueueMessage:
        return QueueMessage(
            event_id=self.event_id,
            team_id=self.team_id,
            user_id=self.user_id,
            channel_id=self.channel_id,
            root_ts=self.root_ts,
            message_ts=self.message_ts,
            question=self.question,
            telemetry=telemetry,
        )

    def event_state(self, received_at: str) -> EventStateEntity:
        return EventStateEntity(event_id=self.event_id, received_at=received_at)


SlackEventSelection = SlackChallenge | IgnoredSlackEvent | AcceptedSlackEvent


def verify_slack_signature(
    raw_body: bytes,
    timestamp: int,
    signature: str,
    signing_secret: str,
    *,
    now: int,
) -> bool:
    if (
        not isinstance(raw_body, bytes)
        or type(timestamp) is not int
        or type(now) is not int
        or not isinstance(signature, str)
        or not isinstance(signing_secret, str)
        or not signing_secret
        or abs(now - timestamp) > SLACK_SIGNATURE_MAX_AGE_SECONDS
    ):
        return False
    base = f"v0:{timestamp}:".encode() + raw_body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def select_slack_event(
    payload: Mapping[str, Any],
    *,
    allowed_team_id: str,
    allowed_user_id: str,
) -> SlackEventSelection:
    if payload.get("type") == "url_verification":
        challenge = _string(payload.get("challenge"))
        if challenge is None:
            return IgnoredSlackEvent("invalid_challenge")
        return SlackChallenge(challenge)
    if payload.get("type") != "event_callback":
        return IgnoredSlackEvent("unsupported_outer_type")

    team_id = _string(payload.get("team_id"))
    event_id = _string(payload.get("event_id"))
    event = payload.get("event")
    if team_id != allowed_team_id:
        return IgnoredSlackEvent("team_not_allowed")
    if event_id is None or not isinstance(event, Mapping):
        return IgnoredSlackEvent("invalid_event_envelope")
    if event.get("type") != "message" or event.get("channel_type") != "im":
        return IgnoredSlackEvent("not_dm_message")
    if "subtype" in event or "bot_id" in event:
        return IgnoredSlackEvent("automated_message")

    user_id = _string(event.get("user"))
    channel_id = _string(event.get("channel"))
    event_ts = _string(event.get("ts"))
    question = _string(event.get("text"))
    if user_id != allowed_user_id:
        return IgnoredSlackEvent("user_not_allowed")
    if channel_id is None or event_ts is None or question is None:
        return IgnoredSlackEvent("invalid_message")
    root_ts = _string(event.get("thread_ts")) or event_ts
    return AcceptedSlackEvent(
        event_id=event_id,
        team_id=team_id,
        user_id=user_id,
        channel_id=channel_id,
        root_ts=root_ts,
        message_ts=event_ts,
        question=question,
    )


def truncate_slack_markdown(markdown: str, *, limit: int = SLACK_MARKDOWN_LIMIT) -> str:
    if not isinstance(markdown, str) or not markdown.strip():
        raise SlackMessageFormatError("Slack markdown must not be empty")
    if type(limit) is not int or limit <= 0:
        raise SlackMessageFormatError("Slack markdown limit must be positive")
    if len(markdown) <= limit:
        return markdown

    body, separator, sources = markdown.rpartition(_SOURCES_SEPARATOR)
    if not separator:
        return markdown[: limit - 1].rstrip() + "…" if limit > 1 else "…"
    if not sources.strip():
        raise SlackMessageFormatError("Sources block must not be empty")
    source_block = f"## Sources\n{sources}"
    source_size = len(source_block) + 2
    if source_size >= limit:
        raise SlackMessageFormatError("Sources block exceeds the Slack markdown limit")
    body_limit = limit - source_size
    shortened_body = body[: body_limit - 1].rstrip() + "…"
    return f"{shortened_body}\n\n{source_block}"


class SlackTransport(Protocol):
    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class SlackWebClient:
    """Minimal Slack Web API surface: an `eyes` receipt and one threaded reply."""

    def __init__(self, transport: SlackTransport) -> None:
        self._transport = transport

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._transport.call(method, payload)
        if response.get("ok") is not True:
            error = response.get("error")
            code = error if isinstance(error, str) and error.strip() else "unknown_error"
            raise SlackApiError(f"Slack {method} failed: {code}")
        return response

    def add_eyes_reaction(self, *, channel_id: str, timestamp: str) -> None:
        self._call(
            "reactions.add",
            {"channel": channel_id, "timestamp": timestamp, "name": "eyes"},
        )

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, markdown: str) -> None:
        reply = truncate_slack_markdown(markdown)
        with traced(
            SPAN_SLACK_MESSAGE_SEND,
            kind=SpanKind.CLIENT,
            **{"knowledge.markdown_length": len(reply)},
        ):
            self._call(
                "chat.postMessage",
                {
                    "channel": channel_id,
                    "thread_ts": thread_ts,
                    "markdown_text": reply,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )


class EventClaimStore(Protocol):
    def claim(self, event: EventStateEntity) -> bool: ...


class QuestionPublisher(Protocol):
    def publish(self, message: QueueMessage) -> None: ...


@dataclass(frozen=True, slots=True)
class SlackHttpResult:
    """What the HTTP trigger returns, plus the reason worth auditing."""

    status_code: int
    body: str
    audit_reason: str


def new_trace_context() -> TraceContext:
    """Start a sampled trace when no upstream context exists; Slack sends none."""
    return TraceContext(traceparent=f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01")


def _utc_timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Slack clock must return a UTC datetime")
    return value.isoformat().replace("+00:00", "Z")


def handle_slack_request(
    *,
    raw_body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    signing_secret: str,
    allowed_team_id: str,
    allowed_user_id: str,
    event_store: EventClaimStore,
    publisher: QuestionPublisher,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    trace_context: Callable[[], TraceContext] = new_trace_context,
) -> SlackHttpResult:
    """Validate, deduplicate, and enqueue one Slack request.

    Every outcome that Slack must not retry returns 2xx. Storage failures are left to
    propagate so the caller answers 5xx and Slack redelivers the event.
    """
    try:
        timestamp = int(timestamp_header) if timestamp_header is not None else None
    except ValueError:
        timestamp = None
    if (
        timestamp is None
        or signature_header is None
        or not verify_slack_signature(
            raw_body,
            timestamp,
            signature_header,
            signing_secret,
            now=int(now().timestamp()),
        )
    ):
        return SlackHttpResult(401, "", "invalid_signature")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return SlackHttpResult(400, "", "invalid_json")
    if not isinstance(payload, Mapping):
        return SlackHttpResult(400, "", "invalid_json")

    selection = select_slack_event(
        payload,
        allowed_team_id=allowed_team_id,
        allowed_user_id=allowed_user_id,
    )
    if isinstance(selection, SlackChallenge):
        return SlackHttpResult(200, selection.challenge, "url_verification")
    if isinstance(selection, IgnoredSlackEvent):
        return SlackHttpResult(200, "", _AUDIT_REASONS.get(selection.reason, selection.reason))

    if not event_store.claim(selection.event_state(_utc_timestamp(now))):
        return SlackHttpResult(200, "", "duplicate_event")
    publisher.publish(selection.queue_message(trace_context()))
    return SlackHttpResult(200, "", "accepted")
