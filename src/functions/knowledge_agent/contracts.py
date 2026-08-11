"""SDK-independent wire and persistence contracts used by the Function App."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

STATE_TABLE_NAME = "state"
SLACK_QUEUE_NAME = "slack-questions"
COSMOS_DATABASE_NAME = "knowledge"
COSMOS_CONTAINER_NAME = "chunks"
CORPUS_ID = "default"
EMBEDDING_DIMENSIONS = 1536

SYNC_PARTITION = "sync"
SYNC_ROW_KEY = "github"
EVENT_PARTITION = "event"
CONVERSATION_PARTITION = "conversation"
SYNC_RUN_RESULTS = frozenset({"success", "partial", "failed"})

_TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class ContractError(ValueError):
    """Raised when a value does not conform to a shared contract."""


class SettingName(StrEnum):
    """Environment variable names shared by adapters and deployment wiring."""

    AZURE_STORAGE_ACCOUNT_NAME = "AZURE_STORAGE_ACCOUNT_NAME"
    COSMOS_ENDPOINT = "COSMOS_ENDPOINT"
    FOUNDRY_PROJECT_ENDPOINT = "FOUNDRY_PROJECT_ENDPOINT"
    EMBEDDING_MODEL_DEPLOYMENT_NAME = "EMBEDDING_MODEL_DEPLOYMENT_NAME"
    KNOWLEDGE_AGENT_ENDPOINT = "KNOWLEDGE_AGENT_ENDPOINT"
    GITHUB_OWNER = "GITHUB_OWNER"
    GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
    GITHUB_DEFAULT_BRANCH = "GITHUB_DEFAULT_BRANCH"
    SLACK_ALLOWED_TEAM_ID = "SLACK_ALLOWED_TEAM_ID"
    SLACK_ALLOWED_USER_ID = "SLACK_ALLOWED_USER_ID"
    SLACK_SIGNING_SECRET = "SLACK_SIGNING_SECRET"
    SLACK_BOT_TOKEN = "SLACK_BOT_TOKEN"
    CHUNKING_VERSION = "CHUNKING_VERSION"


REQUIRED_SETTING_NAMES = tuple(item.value for item in SettingName)


def _require_keys(data: Mapping[str, Any], expected: set[str], contract: str) -> None:
    actual = set(data)
    missing = expected - actual
    unknown = actual - expected
    if missing or unknown:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if unknown:
            details.append(f"unknown={sorted(unknown)}")
        raise ContractError(f"Invalid {contract} keys: {', '.join(details)}")


def _require_non_empty(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")


def _require_utc_timestamp(value: str, field: str) -> None:
    _require_non_empty(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError(f"{field} must be an ISO 8601 timestamp") from error
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ContractError(f"{field} must use UTC")


@dataclass(frozen=True, slots=True)
class TraceContext:
    traceparent: str
    tracestate: str | None = None

    def __post_init__(self) -> None:
        match = _TRACEPARENT_PATTERN.fullmatch(self.traceparent)
        if not match or match.group("version") == "ff":
            raise ContractError("traceparent is not a supported W3C trace context")
        if int(match.group("trace_id"), 16) == 0 or int(match.group("span_id"), 16) == 0:
            raise ContractError("traceparent IDs must not be all zeroes")
        if self.tracestate is not None:
            _require_non_empty(self.tracestate, "tracestate")

    def to_dict(self) -> dict[str, str]:
        value = {"traceparent": self.traceparent}
        if self.tracestate is not None:
            value["tracestate"] = self.tracestate
        return value

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TraceContext:
        expected = {"traceparent"}
        if "tracestate" in data:
            expected.add("tracestate")
        _require_keys(data, expected, "trace context")
        return cls(traceparent=data["traceparent"], tracestate=data.get("tracestate"))


@dataclass(frozen=True, slots=True)
class QueueMessage:
    event_id: str
    team_id: str
    user_id: str
    channel_id: str
    root_ts: str
    message_ts: str
    question: str
    telemetry: TraceContext

    def __post_init__(self) -> None:
        for field, value in (
            ("eventId", self.event_id),
            ("teamId", self.team_id),
            ("userId", self.user_id),
            ("channelId", self.channel_id),
            ("rootTs", self.root_ts),
            ("messageTs", self.message_ts),
            ("question", self.question),
        ):
            _require_non_empty(value, field)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "teamId": self.team_id,
            "userId": self.user_id,
            "channelId": self.channel_id,
            "rootTs": self.root_ts,
            "messageTs": self.message_ts,
            "question": self.question,
            "telemetry": self.telemetry.to_dict(),
        }

    @property
    def correlation_id(self) -> str:
        """Use Slack's globally unique event ID as the end-to-end correlation ID."""
        return self.event_id

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> QueueMessage:
        _require_keys(
            data,
            {
                "eventId",
                "teamId",
                "userId",
                "channelId",
                "rootTs",
                "messageTs",
                "question",
                "telemetry",
            },
            "queue message",
        )
        telemetry = data["telemetry"]
        if not isinstance(telemetry, Mapping):
            raise ContractError("telemetry must be an object")
        return cls(
            event_id=data["eventId"],
            team_id=data["teamId"],
            user_id=data["userId"],
            channel_id=data["channelId"],
            root_ts=data["rootTs"],
            message_ts=data["messageTs"],
            question=data["question"],
            telemetry=TraceContext.from_dict(telemetry),
        )


def conversation_row_key(team_id: str, channel_id: str, root_ts: str) -> str:
    for field, value in (("teamId", team_id), ("channelId", channel_id), ("rootTs", root_ts)):
        _require_non_empty(value, field)
    raw_key = f"{team_id}:{channel_id}:{root_ts}".encode()
    return hashlib.sha256(raw_key).hexdigest()


@dataclass(frozen=True, slots=True)
class SyncStateEntity:
    last_successful_sha: str | None
    last_run_at: str
    last_run_result: str

    def to_entity(self) -> dict[str, str]:
        if self.last_successful_sha is not None and not _GIT_SHA_PATTERN.fullmatch(
            self.last_successful_sha
        ):
            raise ContractError("lastSuccessfulSha must be a 40-character Git SHA")
        _require_non_empty(self.last_run_result, "lastRunResult")
        if self.last_run_result not in SYNC_RUN_RESULTS:
            raise ContractError("lastRunResult must be success, partial, or failed")
        _require_utc_timestamp(self.last_run_at, "lastRunAt")
        entity = {
            "PartitionKey": SYNC_PARTITION,
            "RowKey": SYNC_ROW_KEY,
            "lastRunAt": self.last_run_at,
            "lastRunResult": self.last_run_result,
        }
        if self.last_successful_sha is not None:
            entity["lastSuccessfulSha"] = self.last_successful_sha
        return entity


@dataclass(frozen=True, slots=True)
class EventStateEntity:
    event_id: str
    received_at: str

    def to_entity(self) -> dict[str, str]:
        _require_non_empty(self.event_id, "eventId")
        _require_utc_timestamp(self.received_at, "receivedAt")
        return {
            "PartitionKey": EVENT_PARTITION,
            "RowKey": self.event_id,
            "receivedAt": self.received_at,
        }


@dataclass(frozen=True, slots=True)
class ConversationStateEntity:
    thread_key_hash: str
    response_id: str
    updated_at: str

    def to_entity(self) -> dict[str, str]:
        if not _SHA256_PATTERN.fullmatch(self.thread_key_hash):
            raise ContractError("threadKeyHash must be a lowercase SHA-256 value")
        _require_non_empty(self.response_id, "responseId")
        _require_utc_timestamp(self.updated_at, "updatedAt")
        return {
            "PartitionKey": CONVERSATION_PARTITION,
            "RowKey": self.thread_key_hash,
            "responseId": self.response_id,
            "updatedAt": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class CosmosChunk:
    id: str
    corpus_id: str
    article_id: str
    chunk_index: int
    slug: str
    title: str
    emoji: str
    article_type: str
    topics: tuple[str, ...]
    published: bool
    published_at: str | None
    heading: str | None
    source_path: str
    source_url: str
    source_revision: str
    source_blob_sha: str
    chunking_version: str
    indexed_at: str
    text: str
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.corpus_id != CORPUS_ID:
            raise ContractError(f"corpusId must be {CORPUS_ID!r}")
        if type(self.chunk_index) is not int or self.chunk_index < 0:
            raise ContractError("chunkIndex must not be negative")
        if self.id != f"{self.article_id}:{self.chunk_index}":
            raise ContractError("id must equal articleId:chunkIndex")
        if self.article_id != self.slug:
            raise ContractError("articleId must equal slug")
        for field, value in (
            ("articleId", self.article_id),
            ("slug", self.slug),
            ("title", self.title),
            ("emoji", self.emoji),
            ("articleType", self.article_type),
            ("sourcePath", self.source_path),
            ("sourceUrl", self.source_url),
            ("sourceRevision", self.source_revision),
            ("sourceBlobSha", self.source_blob_sha),
            ("chunkingVersion", self.chunking_version),
            ("text", self.text),
        ):
            _require_non_empty(value, field)
        if self.article_type not in {"tech", "idea"}:
            raise ContractError("articleType must be 'tech' or 'idea'")
        if any(not isinstance(topic, str) or not topic.strip() for topic in self.topics):
            raise ContractError("topics must contain only non-empty strings")
        if not isinstance(self.published, bool):
            raise ContractError("published must be a boolean")
        if self.published_at is not None:
            _require_utc_timestamp(self.published_at, "publishedAt")
        if self.heading is not None:
            _require_non_empty(self.heading, "heading")
        if not _GIT_SHA_PATTERN.fullmatch(self.source_revision):
            raise ContractError("sourceRevision must be a 40-character Git SHA")
        if not _GIT_SHA_PATTERN.fullmatch(self.source_blob_sha):
            raise ContractError("sourceBlobSha must be a 40-character Git SHA")
        _require_utc_timestamp(self.indexed_at, "indexedAt")
        if len(self.embedding) != EMBEDDING_DIMENSIONS:
            raise ContractError(f"embedding must contain {EMBEDDING_DIMENSIONS} values")
        if any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in self.embedding
        ):
            raise ContractError("embedding values must be numeric")
        if any(not math.isfinite(value) for value in self.embedding):
            raise ContractError("embedding values must be finite")

    def to_document(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "corpusId": self.corpus_id,
            "articleId": self.article_id,
            "chunkIndex": self.chunk_index,
            "slug": self.slug,
            "title": self.title,
            "emoji": self.emoji,
            "articleType": self.article_type,
            "topics": list(self.topics),
            "published": self.published,
            "publishedAt": self.published_at,
            "heading": self.heading,
            "sourcePath": self.source_path,
            "sourceUrl": self.source_url,
            "sourceRevision": self.source_revision,
            "sourceBlobSha": self.source_blob_sha,
            "chunkingVersion": self.chunking_version,
            "indexedAt": self.indexed_at,
            "text": self.text,
            "embedding": list(self.embedding),
        }
