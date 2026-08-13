"""Agent Worker handler over injected agent, conversation state, and Slack ports."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from knowledge_agent.contracts import ConversationStateEntity, QueueMessage, conversation_row_key
from knowledge_agent.state import ConversationState, is_conversation_continuable
from knowledge_agent.telemetry import trace_headers

logger = logging.getLogger(__name__)


class AgentInvocationError(RuntimeError):
    """Sanitized Hosted Agent failure that is safe for Function host logs."""


@dataclass(frozen=True, slots=True)
class AgentAnswer:
    response_id: str
    text: str


class HostedAgent(Protocol):
    def ask(self, question: str, *, previous_response_id: str | None) -> AgentAnswer: ...


class ConversationStore(Protocol):
    def get(self, thread_key_hash: str) -> ConversationState | None: ...

    def put(self, state: ConversationStateEntity) -> None: ...


class SlackReplier(Protocol):
    def add_eyes_reaction(self, *, channel_id: str, timestamp: str) -> None: ...

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, markdown: str) -> None: ...


class HostedAgentClient:
    """Calls the Responses endpoint and keeps only the fields the Worker needs."""

    def __init__(self, openai_client: Any) -> None:
        self._client = openai_client

    def ask(self, question: str, *, previous_response_id: str | None) -> AgentAnswer:
        request: dict[str, Any] = {"input": question}
        if previous_response_id is not None:
            request["previous_response_id"] = previous_response_id
        # AIProjectInstrumentor traces responses.create itself, producing a client span
        # that carries gen_ai.response.id, the messages, and the duration. A span of our
        # own around this call measured the identical interval and repeated the response
        # id under a second name, so it is gone.
        request["extra_headers"] = trace_headers()
        try:
            response = self._client.responses.create(**request)
        except Exception as error:
            # `from None` keeps the response body out of the host log and takes the
            # only clue to the failure with it. The class name separates auth from
            # timeout from throttling and quotes nothing.
            logger.error("agent request failed: %s", type(error).__name__)
            raise AgentInvocationError("Hosted Agent request failed") from None

        response_id = getattr(response, "id", None)
        text = getattr(response, "output_text", None)
        if not isinstance(response_id, str) or not response_id.strip():
            raise AgentInvocationError("Hosted Agent response has no usable id")
        if not isinstance(text, str) or not text.strip():
            raise AgentInvocationError("Hosted Agent response has no usable output")
        return AgentAnswer(response_id=response_id, text=text)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("worker clock must return a UTC datetime")
    return value.isoformat().replace("+00:00", "Z")


def handle_question(
    message: QueueMessage,
    *,
    agent: HostedAgent,
    conversations: ConversationStore,
    slack: SlackReplier,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Answer one Slack question in its thread.

    The conversation reference is stored before the reply is posted. A retry after a
    failed post then continues the same response chain instead of posting twice.
    """
    # The receipt is a convenience; losing it must not cost the answer.
    with suppress(Exception):
        slack.add_eyes_reaction(channel_id=message.channel_id, timestamp=message.message_ts)

    thread_key = conversation_row_key(message.team_id, message.channel_id, message.root_ts)
    current = now()
    previous = conversations.get(thread_key)
    previous_response_id = (
        previous.response_id if is_conversation_continuable(previous, now=current) else None
    )

    answer = agent.ask(message.question, previous_response_id=previous_response_id)
    conversations.put(
        ConversationStateEntity(
            thread_key_hash=thread_key,
            response_id=answer.response_id,
            updated_at=_utc_timestamp(current),
        )
    )
    slack.post_thread_reply(
        channel_id=message.channel_id,
        thread_ts=message.root_ts,
        markdown=answer.text,
    )
