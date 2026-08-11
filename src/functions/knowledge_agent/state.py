"""Storage adapters for the Slack request path.

Clients are injected so unit tests never contact Azure. Failures are re-raised as
sanitized errors because the Function host writes them to shared logs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from opentelemetry.trace import SpanKind

from knowledge_agent.contracts import (
    CONVERSATION_PARTITION,
    ConversationStateEntity,
    EventStateEntity,
    QueueMessage,
)
from knowledge_agent.telemetry import SPAN_QUEUE_PUBLISH, traced

CONVERSATION_MAX_AGE = timedelta(days=7)


class StorageAdapterError(RuntimeError):
    """Sanitized Azure Storage failure that is safe for Function host logs."""


class StorageContractError(RuntimeError):
    """Raised when a stored entity cannot be mapped to the application contract."""


class TableEventStore:
    """Claims a Slack ``event_id`` exactly once by relying on Insert Entity."""

    def __init__(self, table_client: Any, *, already_exists_error: type[Exception]) -> None:
        self._table = table_client
        self._already_exists_error = already_exists_error

    def claim(self, event: EventStateEntity) -> bool:
        """Return True when this event is new, False when Slack resent it."""
        try:
            self._table.create_entity(entity=event.to_entity())
        except self._already_exists_error:
            return False
        except Exception:
            raise StorageAdapterError("Table event claim failed") from None
        return True


@dataclass(frozen=True, slots=True)
class ConversationState:
    response_id: str
    updated_at: datetime


class TableConversationStore:
    def __init__(self, table_client: Any, *, not_found_error: type[Exception]) -> None:
        self._table = table_client
        self._not_found_error = not_found_error

    def get(self, thread_key_hash: str) -> ConversationState | None:
        try:
            entity = self._table.get_entity(
                partition_key=CONVERSATION_PARTITION,
                row_key=thread_key_hash,
            )
        except self._not_found_error:
            return None
        except Exception:
            raise StorageAdapterError("Table conversation read failed") from None
        if not isinstance(entity, Mapping):
            raise StorageContractError("Table conversation state must be an object")
        state = ConversationStateEntity(
            thread_key_hash=thread_key_hash,
            response_id=entity.get("responseId"),
            updated_at=entity.get("updatedAt"),
        )
        state.to_entity()
        updated_at = datetime.fromisoformat(state.updated_at.replace("Z", "+00:00"))
        return ConversationState(response_id=state.response_id, updated_at=updated_at)

    def put(self, state: ConversationStateEntity) -> None:
        try:
            self._table.upsert_entity(entity=state.to_entity(), mode="replace")
        except Exception:
            raise StorageAdapterError("Table conversation write failed") from None


def is_conversation_continuable(
    state: ConversationState | None,
    *,
    now: datetime,
    max_age: timedelta = CONVERSATION_MAX_AGE,
) -> bool:
    """Drop references older than the safety net so expired IDs are never sent."""
    if state is None:
        return False
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
        raise ValueError("conversation clock must return a UTC datetime")
    return now - state.updated_at < max_age


class QueueQuestionPublisher:
    def __init__(self, queue_client: Any) -> None:
        self._queue = queue_client

    def publish(self, message: QueueMessage) -> None:
        payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with traced(
            SPAN_QUEUE_PUBLISH,
            kind=SpanKind.PRODUCER,
            **{"knowledge.event_id": message.event_id},
        ):
            try:
                self._queue.send_message(payload)
            except Exception:
                raise StorageAdapterError("Queue publish failed") from None
