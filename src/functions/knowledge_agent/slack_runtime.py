"""Lazy production wiring for the Slack HTTP trigger and the Agent Worker.

Each factory imports only the SDKs its own trigger needs, so a Slack event never
pays the cold-start cost of the Agent client and vice versa.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from opentelemetry.trace import SpanKind

from knowledge_agent.contracts import (
    KNOWLEDGE_AGENT_NAME,
    SLACK_QUEUE_NAME,
    STATE_TABLE_NAME,
    QueueMessage,
)
from knowledge_agent.http_transport import SlackHttpTransport
from knowledge_agent.settings import SlackEventsSettings, WorkerSettings
from knowledge_agent.slack_events import (
    SlackHttpResult,
    SlackWebClient,
    handle_slack_request,
    new_trace_context,
)
from knowledge_agent.state import QueueQuestionPublisher, TableConversationStore, TableEventStore
from knowledge_agent.telemetry import (
    SPAN_SLACK_EVENT_RECEIVE,
    continued_trace,
    current_trace_context,
    set_attributes,
    traced,
)
from knowledge_agent.worker import HostedAgentClient, handle_question


def _table_endpoint(storage_account_name: str) -> str:
    return f"https://{storage_account_name}.table.core.windows.net"


@dataclass(frozen=True, slots=True)
class SlackEventsRuntime:
    settings: SlackEventsSettings
    event_store: TableEventStore
    publisher: QueueQuestionPublisher


@lru_cache(maxsize=1)
def build_slack_events_runtime() -> SlackEventsRuntime:
    from azure.core.exceptions import ResourceExistsError
    from azure.data.tables import TableServiceClient
    from azure.identity import DefaultAzureCredential
    from azure.storage.queue import QueueClient, TextBase64EncodePolicy

    settings = SlackEventsSettings.from_environment(os.environ)
    credential = DefaultAzureCredential()
    table = TableServiceClient(
        endpoint=_table_endpoint(settings.storage_account_name),
        credential=credential,
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    ).get_table_client(STATE_TABLE_NAME)
    # Base64 matches the Functions host default in host.json, so the trigger can read it.
    queue = QueueClient(
        account_url=f"https://{settings.storage_account_name}.queue.core.windows.net",
        queue_name=SLACK_QUEUE_NAME,
        credential=credential,
        message_encode_policy=TextBase64EncodePolicy(),
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    )
    return SlackEventsRuntime(
        settings=settings,
        event_store=TableEventStore(table, already_exists_error=ResourceExistsError),
        publisher=QueueQuestionPublisher(queue),
    )


def handle_configured_slack_request(
    *,
    raw_body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
) -> SlackHttpResult:
    runtime = build_slack_events_runtime()
    with traced(SPAN_SLACK_EVENT_RECEIVE, kind=SpanKind.SERVER) as span:
        result = handle_slack_request(
            raw_body=raw_body,
            timestamp_header=timestamp_header,
            signature_header=signature_header,
            signing_secret=runtime.settings.signing_secret,
            allowed_team_id=runtime.settings.allowed_team_id,
            allowed_user_id=runtime.settings.allowed_user_id,
            event_store=runtime.event_store,
            publisher=runtime.publisher,
            # The queue message must carry this span's context, not a fresh trace.
            trace_context=lambda: current_trace_context() or new_trace_context(),
        )
        set_attributes(span, **{"knowledge.audit_reason": result.audit_reason})
        return result


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    settings: WorkerSettings
    agent: HostedAgentClient
    conversations: TableConversationStore
    slack: SlackWebClient


def _agent_client(settings: WorkerSettings, credential: Any) -> HostedAgentClient:
    from azure.ai.projects import AIProjectClient

    # The SDK builds the same Responses URL this used to assemble by hand
    # ({endpoint}/agents/{name}/endpoint/protocols/openai plus api-version=v1) and wires
    # the same bearer token scope. It also registers AIProjectInstrumentor's httpx hook
    # on the returned client, which injects traceparent at request time -- inside the
    # `responses` span rather than before it, so the Agent's spans land as its children.
    # allow_preview is what the docstring requires for agent_name; 2.4.0 does not enforce
    # it, and depending on that gap would make this break on a patch release.
    project = AIProjectClient(
        endpoint=settings.foundry_project_endpoint,
        credential=credential,
        allow_preview=True,
    )
    return HostedAgentClient(
        project.get_openai_client(agent_name=KNOWLEDGE_AGENT_NAME).with_options(
            timeout=120.0,
            max_retries=1,
        )
    )


@lru_cache(maxsize=1)
def build_worker_runtime() -> WorkerRuntime:
    from azure.core.exceptions import ResourceNotFoundError
    from azure.data.tables import TableServiceClient
    from azure.identity import DefaultAzureCredential

    settings = WorkerSettings.from_environment(os.environ)
    credential = DefaultAzureCredential()
    table = TableServiceClient(
        endpoint=_table_endpoint(settings.storage_account_name),
        credential=credential,
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    ).get_table_client(STATE_TABLE_NAME)
    return WorkerRuntime(
        settings=settings,
        agent=_agent_client(settings, credential),
        conversations=TableConversationStore(table, not_found_error=ResourceNotFoundError),
        slack=SlackWebClient(SlackHttpTransport(settings.bot_token)),
    )


def run_configured_worker(payload: str | bytes) -> None:
    message = QueueMessage.from_dict(json.loads(payload))
    runtime = build_worker_runtime()
    # Rejoin the trace the Slack request started so one question is one trace.
    with continued_trace(message.telemetry):
        handle_question(
            message,
            agent=runtime.agent,
            conversations=runtime.conversations,
            slack=runtime.slack,
        )
