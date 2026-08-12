from __future__ import annotations

from types import SimpleNamespace

from knowledge_agent.sync_runtime import build_sync_runtime


def test_runtime_wires_one_managed_identity_to_bounded_sdk_clients(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import azure.ai.projects
    import azure.cosmos
    import azure.data.tables
    import azure.identity

    environment = {
        "AZURE_STORAGE_ACCOUNT_NAME": "techknowledge123",
        "COSMOS_ENDPOINT": "https://example." "documents.azure.com:443/",
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://example." "services.ai.azure.com/api/projects/dev"
        ),
        "EMBEDDING_MODEL_DEPLOYMENT_NAME": "text-embedding-3-small",
        "GITHUB_OWNER": "example-owner",
        "GITHUB_REPOSITORY": "example-repository",
        "GITHUB_DEFAULT_BRANCH": "main",
        "GITHUB_TOKEN": "github_pat_" + "0" * 22 + "_" + "1" * 59,
        "CHUNKING_VERSION": "markdown-v1-1600-200",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    credential = object()
    calls: dict[str, object] = {}

    class FakeTableService:
        def __init__(self, **kwargs: object) -> None:
            calls["table"] = kwargs

        def get_table_client(self, name: str) -> object:
            calls["table_name"] = name
            return object()

    class FakeCosmos:
        def __init__(self, endpoint: str, **kwargs: object) -> None:
            calls["cosmos"] = (endpoint, kwargs)

        def get_database_client(self, name: str) -> object:
            calls["database_name"] = name
            return SimpleNamespace(
                get_container_client=lambda container: calls.setdefault(
                    "container_name", container
                )
            )

    class FakeOpenAI:
        def with_options(self, **kwargs: object) -> object:
            calls["openai_options"] = kwargs
            return SimpleNamespace(embeddings=object())

    class FakeProject:
        def __init__(self, **kwargs: object) -> None:
            calls["project"] = kwargs

        def get_openai_client(self) -> FakeOpenAI:
            return FakeOpenAI()

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(azure.data.tables, "TableServiceClient", FakeTableService)
    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeCosmos)
    monkeypatch.setattr(azure.ai.projects, "AIProjectClient", FakeProject)
    build_sync_runtime.cache_clear()

    runtime = build_sync_runtime()

    assert calls["table_name"] == "state"
    assert calls["database_name"] == "knowledge"
    assert calls["container_name"] == "chunks"
    assert calls["table"]["credential"] is credential  # type: ignore[index]
    assert calls["cosmos"][1]["credential"] is credential  # type: ignore[index]
    assert calls["project"]["credential"] is credential  # type: ignore[index]
    assert calls["table"]["connection_timeout"] == 5  # type: ignore[index]
    assert calls["table"]["read_timeout"] == 30  # type: ignore[index]
    assert calls["cosmos"][1]["connection_timeout"] == 5  # type: ignore[index]
    assert calls["cosmos"][1]["read_timeout"] == 30  # type: ignore[index]
    assert calls["project"]["connection_timeout"] == 5  # type: ignore[index]
    assert calls["project"]["read_timeout"] == 30  # type: ignore[index]
    assert calls["openai_options"] == {"timeout": 30.0, "max_retries": 2}
    assert runtime.settings.chunking_version == "markdown-v1-1600-200"

    build_sync_runtime.cache_clear()
