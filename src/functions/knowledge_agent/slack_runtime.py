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

from knowledge_agent.contracts import SLACK_QUEUE_NAME, STATE_TABLE_NAME, QueueMessage
from knowledge_agent.http_transport import SlackHttpTransport
from knowledge_agent.settings import SlackEventsSettings, WorkerSettings
from knowledge_agent.slack_events import SlackHttpResult, SlackWebClient, handle_slack_request
from knowledge_agent.state import QueueQuestionPublisher, TableConversationStore, TableEventStore
from knowledge_agent.worker import HostedAgentClient, handle_question

AGENT_TOKEN_SCOPE = "https://ai.azure.com/.default"


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
    return handle_slack_request(
        raw_body=raw_body,
        timestamp_header=timestamp_header,
        signature_header=signature_header,
        signing_secret=runtime.settings.signing_secret,
        allowed_team_id=runtime.settings.allowed_team_id,
        allowed_user_id=runtime.settings.allowed_user_id,
        event_store=runtime.event_store,
        publisher=runtime.publisher,
    )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    settings: WorkerSettings
    agent: HostedAgentClient
    conversations: TableConversationStore
    slack: SlackWebClient


def _agent_client(settings: WorkerSettings, credential: Any) -> HostedAgentClient:
    from azure.identity import get_bearer_token_provider
    from openai import AzureOpenAI

    return HostedAgentClient(
        AzureOpenAI(
            base_url=settings.agent_endpoint.base_url,
            api_version=settings.agent_endpoint.api_version,
            azure_ad_token_provider=get_bearer_token_provider(credential, AGENT_TOKEN_SCOPE),
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
    handle_question(
        message,
        agent=runtime.agent,
        conversations=runtime.conversations,
        slack=runtime.slack,
    )
