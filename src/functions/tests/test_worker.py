from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from knowledge_agent.contracts import (
    ConversationStateEntity,
    QueueMessage,
    TraceContext,
    conversation_row_key,
)
from knowledge_agent.state import ConversationState
from knowledge_agent.worker import (
    AgentAnswer,
    AgentInvocationError,
    HostedAgentClient,
    handle_question,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
THREAD_KEY = conversation_row_key("T1", "D1", "1720000000.000001")


def _message(message_ts: str = "1720000100.000002") -> QueueMessage:
    return QueueMessage(
        event_id="Ev1",
        team_id="T1",
        user_id="U1",
        channel_id="D1",
        root_ts="1720000000.000001",
        message_ts=message_ts,
        question="How does the sync work?",
        telemetry=TraceContext("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"),
    )


class FakeAgent:
    def __init__(self, answer: AgentAnswer | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str | None]] = []

    def ask(self, question: str, *, previous_response_id: str | None) -> AgentAnswer:
        self.calls.append((question, previous_response_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeConversations:
    def __init__(self, state: ConversationState | None = None) -> None:
        self.state = state
        self.saved: list[ConversationStateEntity] = []

    def get(self, thread_key_hash: str) -> ConversationState | None:
        assert thread_key_hash == THREAD_KEY
        return self.state

    def put(self, state: ConversationStateEntity) -> None:
        self.saved.append(state)


class FakeSlack:
    def __init__(self, reaction_error: Exception | None = None) -> None:
        self.reaction_error = reaction_error
        self.reactions: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    def add_eyes_reaction(self, *, channel_id: str, timestamp: str) -> None:
        if self.reaction_error is not None:
            raise self.reaction_error
        self.reactions.append((channel_id, timestamp))

    def post_thread_reply(self, *, channel_id: str, thread_ts: str, markdown: str) -> None:
        self.replies.append((channel_id, thread_ts, markdown))


def test_top_level_question_starts_a_new_conversation_and_replies_in_thread() -> None:
    agent = FakeAgent(AgentAnswer("resp_1", "Answer"))
    conversations = FakeConversations()
    slack = FakeSlack()

    handle_question(
        _message(),
        agent=agent,
        conversations=conversations,
        slack=slack,
        now=lambda: NOW,
    )

    assert agent.calls == [("How does the sync work?", None)]
    # The receipt lands on the asking message, not on the thread parent.
    assert slack.reactions == [("D1", "1720000100.000002")]
    assert slack.replies == [("D1", "1720000000.000001", "Answer")]
    entity = conversations.saved[0].to_entity()
    assert entity["responseId"] == "resp_1"
    assert entity["updatedAt"] == "2026-08-11T00:00:00Z"
    assert entity["RowKey"] == THREAD_KEY


def test_follow_up_continues_only_while_the_reference_is_fresh() -> None:
    fresh = FakeConversations(ConversationState("resp_1", NOW - timedelta(days=6)))
    agent = FakeAgent(AgentAnswer("resp_2", "Answer"))
    handle_question(
        _message(),
        agent=agent,
        conversations=fresh,
        slack=FakeSlack(),
        now=lambda: NOW,
    )
    assert agent.calls == [("How does the sync work?", "resp_1")]

    expired = FakeConversations(ConversationState("resp_1", NOW - timedelta(days=7)))
    restarted = FakeAgent(AgentAnswer("resp_3", "Answer"))
    handle_question(
        _message(),
        agent=restarted,
        conversations=expired,
        slack=FakeSlack(),
        now=lambda: NOW,
    )
    assert restarted.calls == [("How does the sync work?", None)]


def test_reaction_failure_never_costs_the_answer() -> None:
    slack = FakeSlack(reaction_error=RuntimeError("already_reacted"))
    conversations = FakeConversations()

    handle_question(
        _message(),
        agent=FakeAgent(AgentAnswer("resp_1", "Answer")),
        conversations=conversations,
        slack=slack,
        now=lambda: NOW,
    )

    assert slack.replies == [("D1", "1720000000.000001", "Answer")]
    assert conversations.saved


def test_agent_failure_leaves_no_conversation_reference_and_no_reply() -> None:
    conversations = FakeConversations()
    slack = FakeSlack()

    with pytest.raises(AgentInvocationError):
        handle_question(
            _message(),
            agent=FakeAgent(AgentInvocationError("Hosted Agent request failed")),
            conversations=conversations,
            slack=slack,
            now=lambda: NOW,
        )

    assert conversations.saved == []
    assert slack.replies == []


class FakeResponses:
    def __init__(self, result: object) -> None:
        self.result = result
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeOpenAI:
    def __init__(self, result: object) -> None:
        self.responses = FakeResponses(result)


class FakeResponse:
    def __init__(self, response_id: object, output_text: object) -> None:
        self.id = response_id
        self.output_text = output_text


def test_agent_client_sends_previous_response_id_only_when_continuing() -> None:
    client = FakeOpenAI(FakeResponse("resp_1", "Answer"))
    agent = HostedAgentClient(client)

    assert agent.ask("Q", previous_response_id=None) == AgentAnswer("resp_1", "Answer")
    assert client.responses.requests[0] == {"input": "Q"}

    agent.ask("Q2", previous_response_id="resp_1")
    assert client.responses.requests[1] == {"input": "Q2", "previous_response_id": "resp_1"}


def test_agent_client_rejects_unusable_responses_and_sanitizes_failures() -> None:
    with pytest.raises(AgentInvocationError, match="no usable id"):
        HostedAgentClient(FakeOpenAI(FakeResponse("", "Answer"))).ask(
            "Q", previous_response_id=None
        )

    with pytest.raises(AgentInvocationError, match="no usable output"):
        HostedAgentClient(FakeOpenAI(FakeResponse("resp_1", "  "))).ask(
            "Q", previous_response_id=None
        )

    failing = HostedAgentClient(FakeOpenAI(RuntimeError("bearer eyJhbGciOi")))
    with pytest.raises(AgentInvocationError) as captured:
        failing.ask("Q", previous_response_id=None)
    assert "eyJhbGciOi" not in str(captured.value)


def test_configured_client_targets_the_agent_responses_url_with_a_managed_identity_token() -> None:
    """Lock the wiring: split endpoint + AzureOpenAI must rebuild the deployed URL."""
    from openai import AzureOpenAI
    from openai._models import FinalRequestOptions

    from knowledge_agent.settings import WorkerSettings

    endpoint = (
        "https://example." "services.ai.azure.com/api/projects/dev/agents/knowledge-agent"
        "/endpoint/protocols/openai/responses?api-version=v1"
    )
    settings = WorkerSettings.from_environment(
        {
            "AZURE_STORAGE_ACCOUNT_NAME": "techknowledge123",
            "KNOWLEDGE_AGENT_ENDPOINT": endpoint,
            "SLACK_BOT_TOKEN": "xoxb-0123456789-abcdefghij",
        }
    )
    client = AzureOpenAI(
        base_url=settings.agent_endpoint.base_url,
        api_version=settings.agent_endpoint.api_version,
        azure_ad_token_provider=lambda: "managed-identity-token",
    )

    options = client._prepare_options(
        FinalRequestOptions.construct(method="post", url="/responses", json_data={"input": "Q"})
    )
    request = client._build_request(options)

    assert str(request.url) == endpoint
    assert request.headers["Authorization"] == "Bearer managed-identity-token"
