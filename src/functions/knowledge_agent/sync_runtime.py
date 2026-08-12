"""Lazy production wiring for the synchronization Function."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

from knowledge_agent.azure_adapters import (
    CosmosIndexRepository,
    FoundryEmbeddingProvider,
    TableSyncStateStore,
)
from knowledge_agent.contracts import COSMOS_CONTAINER_NAME, COSMOS_DATABASE_NAME, STATE_TABLE_NAME
from knowledge_agent.github_source import GitHubSourceClient
from knowledge_agent.http_transport import GitHubHttpTransport
from knowledge_agent.settings import SyncSettings
from knowledge_agent.sync_function import run_sync


def account_openai_base_url(project_endpoint: str) -> str:
    """Return the account-level OpenAI base URL that actually serves embeddings.

    The project-scoped route the SDK builds by default
    (``/api/projects/<project>/openai/v1/``) serves chat completions but answers 404 for
    embeddings, so the embedding client has to target the account root instead. Passing
    base_url keeps the SDK's own token handling.
    """
    parts = urlsplit(project_endpoint)
    return f"{parts.scheme}://{parts.netloc}/openai/v1/"


@dataclass(frozen=True, slots=True)
class SyncRuntime:
    settings: SyncSettings
    source: GitHubSourceClient
    index: CosmosIndexRepository
    embedder: FoundryEmbeddingProvider
    state_store: TableSyncStateStore


@lru_cache(maxsize=1)
def build_sync_runtime() -> SyncRuntime:
    # Imports stay inside the timer-only factory so Slack's future HTTP path does not
    # pay the cold-start cost of Cosmos and Foundry SDKs.
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.cosmos import CosmosClient
    from azure.data.tables import TableServiceClient
    from azure.identity import DefaultAzureCredential

    settings = SyncSettings.from_environment(os.environ)
    credential = DefaultAzureCredential()
    table_service = TableServiceClient(
        endpoint=f"https://{settings.storage_account_name}.table.core.windows.net",
        credential=credential,
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    )
    table = table_service.get_table_client(STATE_TABLE_NAME)
    cosmos = CosmosClient(
        settings.cosmos_endpoint,
        credential=credential,
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    )
    container = cosmos.get_database_client(COSMOS_DATABASE_NAME).get_container_client(
        COSMOS_CONTAINER_NAME
    )
    project = AIProjectClient(
        endpoint=settings.foundry_project_endpoint,
        credential=credential,
        connection_timeout=5,
        read_timeout=30,
        retry_total=2,
    )
    openai_client = project.get_openai_client(
        base_url=account_openai_base_url(settings.foundry_project_endpoint),
    ).with_options(
        timeout=30.0,
        max_retries=2,
    )
    return SyncRuntime(
        settings=settings,
        source=GitHubSourceClient(
            settings.github_owner,
            settings.github_repository,
            settings.github_default_branch,
            GitHubHttpTransport(settings.github_token),
        ),
        index=CosmosIndexRepository(container),
        embedder=FoundryEmbeddingProvider(openai_client, settings.embedding_deployment_name),
        state_store=TableSyncStateStore(table, not_found_error=ResourceNotFoundError),
    )


def run_configured_sync() -> None:
    runtime = build_sync_runtime()
    run_sync(
        source=runtime.source,
        index=runtime.index,
        embedder=runtime.embedder,
        state_store=runtime.state_store,
        chunking_version=runtime.settings.chunking_version,
    )
