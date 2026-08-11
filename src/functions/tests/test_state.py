from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from knowledge_agent.contracts import (
    ConversationStateEntity,
    EventStateEntity,
    QueueMessage,
    TraceContext,
)
from knowledge_agent.state import (
    ConversationState,
    QueueQuestionPublisher,
    StorageAdapterError,
    TableConversationStore,
    TableEventStore,
    is_conversation_continuable,
)

THREAD_KEY = "466b18d1b4db8b9f2f0f4c7c88feb6155fadca2fce58d1b235506017e693691d"


class AlreadyExists(Exception):
    pass


class NotFound(Exception):
    pass


class FakeTable:
    def __init__(self, entity: dict[str, str] | None = None, error: Exception | None = None):
        self.entity = entity
        self.error = error
        self.created: list[dict[str, str]] = []
        self.upserted: list[dict[str, str]] = []

    def create_entity(self, *, entity: dict[str, str]) -> None:
        if self.error is not None:
            raise self.error
        self.created.append(entity)

    def get_entity(self, *, partition_key: str, row_key: str) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        if self.entity is None:
            raise NotFound()
        return self.entity

    def upsert_entity(self, *, entity: dict[str, str], mode: str) -> None:
        if self.error is not None:
            raise self.error
        assert mode == "replace"
        self.upserted.append(entity)


def _event() -> EventStateEntity:
    return EventStateEntity(event_id="Ev1", received_at="2026-08-11T00:00:00Z")


def test_event_claim_succeeds_once_and_reports_slack_redelivery() -> None:
    table = FakeTable()
    store = TableEventStore(table, already_exists_error=AlreadyExists)
    assert store.claim(_event()) is True
    assert table.created[0]["RowKey"] == "Ev1"

    duplicate = TableEventStore(
        FakeTable(error=AlreadyExists()), already_exists_error=AlreadyExists
    )
    assert duplicate.claim(_event()) is False


def test_event_claim_sanitizes_unexpected_storage_failures() -> None:
    store = TableEventStore(
        FakeTable(error=RuntimeError("account-key-leak")),
        already_exists_error=AlreadyExists,
    )
    with pytest.raises(StorageAdapterError) as captured:
        store.claim(_event())
    assert "account-key-leak" not in str(captured.value)


def test_conversation_roundtrip_validates_the_stored_entity() -> None:
    table = FakeTable(
        {
            "PartitionKey": "conversation",
            "RowKey": THREAD_KEY,
            "responseId": "resp_1",
            "updatedAt": "2026-08-11T00:00:00Z",
        }
    )
    store = TableConversationStore(table, not_found_error=NotFound)

    state = store.get(THREAD_KEY)
    assert state == ConversationState(
        response_id="resp_1",
        updated_at=datetime(2026, 8, 11, tzinfo=UTC),
    )
    assert TableConversationStore(FakeTable(), not_found_error=NotFound).get(THREAD_KEY) is None

    store.put(
        ConversationStateEntity(
            thread_key_hash=THREAD_KEY,
            response_id="resp_2",
            updated_at="2026-08-11T00:05:00Z",
        )
    )
    assert table.upserted[0]["responseId"] == "resp_2"


def test_conversation_reference_expires_after_seven_days() -> None:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    fresh = ConversationState("resp_1", now - timedelta(days=7) + timedelta(seconds=1))
    stale = ConversationState("resp_1", now - timedelta(days=7))

    assert is_conversation_continuable(fresh, now=now) is True
    assert is_conversation_continuable(stale, now=now) is False
    assert is_conversation_continuable(None, now=now) is False
    with pytest.raises(ValueError):
        is_conversation_continuable(fresh, now=now.replace(tzinfo=None))


class FakeQueue:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.messages: list[str] = []

    def send_message(self, content: str) -> None:
        if self.error is not None:
            raise self.error
        self.messages.append(content)


def _message() -> QueueMessage:
    return QueueMessage(
        event_id="Ev1",
        team_id="T1",
        user_id="U1",
        channel_id="D1",
        root_ts="1720000000.000001",
        message_ts="1720000100.000002",
        question="日本語の質問",
        telemetry=TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
    )


def test_queue_publishes_the_wire_contract_without_escaping_unicode() -> None:
    queue = FakeQueue()
    QueueQuestionPublisher(queue).publish(_message())

    assert "日本語の質問" in queue.messages[0]
    assert json.loads(queue.messages[0]) == _message().to_dict()

    failing = QueueQuestionPublisher(FakeQueue(error=RuntimeError("sas-token")))
    with pytest.raises(StorageAdapterError) as captured:
        failing.publish(_message())
    assert "sas-token" not in str(captured.value)
