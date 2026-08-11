"""Pure Slack request validation, event selection, and message formatting."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from knowledge_agent.contracts import EventStateEntity, QueueMessage, TraceContext

SLACK_SIGNATURE_MAX_AGE_SECONDS = 300
SLACK_MARKDOWN_LIMIT = 4000
_SOURCES_SEPARATOR = "\n\n## Sources\n"


class SlackMessageFormatError(ValueError):
    """Raised when a Slack response cannot preserve its source contract."""


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
    question: str

    def queue_message(self, telemetry: TraceContext) -> QueueMessage:
        return QueueMessage(
            event_id=self.event_id,
            team_id=self.team_id,
            user_id=self.user_id,
            channel_id=self.channel_id,
            root_ts=self.root_ts,
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
