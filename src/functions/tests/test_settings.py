from __future__ import annotations

import pytest

from knowledge_agent.settings import SettingsError, SyncSettings


def _environment() -> dict[str, str]:
    return {
        "AZURE_STORAGE_ACCOUNT_NAME": "techknowledge123",
        "COSMOS_ENDPOINT": "https://example." "documents.azure.com:443/",
        "FOUNDRY_PROJECT_ENDPOINT": (
            "https://example." "services.ai.azure.com/api/projects/dev"
        ),
        "EMBEDDING_MODEL_DEPLOYMENT_NAME": "text-embedding-3-small",
        "GITHUB_OWNER": "example-owner",
        "GITHUB_REPOSITORY": "example.repository",
        "GITHUB_DEFAULT_BRANCH": "main",
        "CHUNKING_VERSION": "markdown-v1-1600-200",
    }


def test_sync_settings_validate_and_normalize_endpoints() -> None:
    settings = SyncSettings.from_environment(_environment())

    assert settings.cosmos_endpoint == "https://example." "documents.azure.com:443"
    assert settings.foundry_project_endpoint.endswith("/api/projects/dev")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COSMOS_ENDPOINT", "http://example.documents.azure.com"),
        ("COSMOS_ENDPOINT", "https://example.invalid"),
        ("COSMOS_ENDPOINT", "https://example." "documents.azure.com/not-account-root"),
        (
            "FOUNDRY_PROJECT_ENDPOINT",
            "https://user:password@example." "services.ai.azure.com",
        ),
        (
            "FOUNDRY_PROJECT_ENDPOINT",
            "https://example." "services.ai.azure.com/api/not-projects/dev",
        ),
        ("AZURE_STORAGE_ACCOUNT_NAME", "INVALID-NAME"),
        ("GITHUB_OWNER", "owner/name"),
    ],
)
def test_sync_settings_fail_closed_without_echoing_values(name: str, value: str) -> None:
    environment = _environment()
    environment[name] = value

    with pytest.raises(SettingsError) as captured:
        SyncSettings.from_environment(environment)

    assert value not in str(captured.value)
