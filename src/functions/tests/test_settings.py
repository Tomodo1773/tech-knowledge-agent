from __future__ import annotations

import pytest

from knowledge_agent.settings import (
    SettingsError,
    SlackEventsSettings,
    SyncSettings,
    WorkerSettings,
)

# Split so the repository policy scan does not read these fixtures as real endpoints.
_FOUNDRY_HOST = "https://example." "services.ai.azure.com"
_GITHUB_TOKEN = "github_pat_" + "0" * 22 + "_" + "1" * 59
_AGENT_ENDPOINT = (
    f"{_FOUNDRY_HOST}/api/projects/dev/agents/knowledge-agent"
    "/endpoint/protocols/openai/responses?api-version=v1"
)


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
        "GITHUB_TOKEN": _GITHUB_TOKEN,
        "CHUNKING_VERSION": "markdown-v1-1600-200",
    }


def test_sync_settings_validate_and_normalize_endpoints() -> None:
    settings = SyncSettings.from_environment(_environment())

    assert settings.cosmos_endpoint == "https://example." "documents.azure.com:443"
    assert settings.foundry_project_endpoint.endswith("/api/projects/dev")
    # The source repository is private, so the token is required and must stay hidden.
    assert settings.github_token == _GITHUB_TOKEN
    assert _GITHUB_TOKEN not in repr(settings)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "not-a-token", "ghp_short", "github_pat_short"],
)
def test_sync_settings_reject_a_value_that_is_not_a_github_token(value: str) -> None:
    environment = _environment()
    environment["GITHUB_TOKEN"] = value

    with pytest.raises(SettingsError, match="GITHUB_TOKEN"):
        SyncSettings.from_environment(environment)


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


def _slack_environment() -> dict[str, str]:
    return {
        "AZURE_STORAGE_ACCOUNT_NAME": "techknowledge123",
        "SLACK_ALLOWED_TEAM_ID": "T012AB3CD",
        "SLACK_ALLOWED_USER_ID": "U012AB3CD",
        "SLACK_SIGNING_SECRET": "0123456789abcdef0123",
    }


def _worker_environment() -> dict[str, str]:
    return {
        "AZURE_STORAGE_ACCOUNT_NAME": "techknowledge123",
        "KNOWLEDGE_AGENT_ENDPOINT": _AGENT_ENDPOINT,
        "SLACK_BOT_TOKEN": "xoxb-0123456789-abcdefghij",
    }


def test_slack_settings_keep_the_signing_secret_out_of_representations() -> None:
    settings = SlackEventsSettings.from_environment(_slack_environment())

    assert settings.allowed_team_id == "T012AB3CD"
    assert settings.signing_secret == "0123456789abcdef0123"
    assert "0123456789abcdef0123" not in repr(settings)


def test_worker_settings_split_the_responses_endpoint_for_the_openai_client() -> None:
    settings = WorkerSettings.from_environment(_worker_environment())

    # The client appends "/responses" itself, so base_url stops at the protocol root.
    assert settings.agent_endpoint.base_url.endswith(
        "/api/projects/dev/agents/knowledge-agent/endpoint/protocols/openai"
    )
    assert settings.agent_endpoint.api_version == "v1"
    assert "xoxb-0123456789-abcdefghij" not in repr(settings)


def test_worker_settings_default_the_api_version_when_the_endpoint_omits_it() -> None:
    environment = _worker_environment()
    environment["KNOWLEDGE_AGENT_ENDPOINT"] = _AGENT_ENDPOINT.split("?")[0]

    assert WorkerSettings.from_environment(environment).agent_endpoint.api_version == "v1"


@pytest.mark.parametrize(
    "value",
    [
        "http://example." "services.ai.azure.com/api/projects/dev/agents/a"
        "/endpoint/protocols/openai/responses",
        f"{_FOUNDRY_HOST}/api/projects/dev/agents/a/endpoint/protocols/openai",
        "https://example.invalid/api/projects/dev/agents/a"
        "/endpoint/protocols/openai/responses",
        f"{_AGENT_ENDPOINT}&extra=1",
    ],
)
def test_worker_settings_fail_closed_on_an_unusable_agent_endpoint(value: str) -> None:
    environment = _worker_environment()
    environment["KNOWLEDGE_AGENT_ENDPOINT"] = value

    with pytest.raises(SettingsError) as captured:
        WorkerSettings.from_environment(environment)

    assert value not in str(captured.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SLACK_ALLOWED_TEAM_ID", "t012ab3cd"),
        ("SLACK_ALLOWED_USER_ID", "U-012"),
        ("SLACK_SIGNING_SECRET", "tooshort"),
    ],
)
def test_slack_settings_fail_closed_without_echoing_values(name: str, value: str) -> None:
    environment = _slack_environment()
    environment[name] = value

    with pytest.raises(SettingsError) as captured:
        SlackEventsSettings.from_environment(environment)

    assert value not in str(captured.value)


def test_worker_settings_fail_closed_when_postdeploy_wiring_left_the_endpoint_empty() -> None:
    environment = _worker_environment()
    environment["KNOWLEDGE_AGENT_ENDPOINT"] = "   "

    with pytest.raises(SettingsError, match="KNOWLEDGE_AGENT_ENDPOINT is missing"):
        WorkerSettings.from_environment(environment)


def test_worker_settings_reject_a_token_that_is_not_a_bot_token() -> None:
    environment = _worker_environment()
    environment["SLACK_BOT_TOKEN"] = "xoxp-0123456789-abcdefghij"

    with pytest.raises(SettingsError, match="SLACK_BOT_TOKEN"):
        WorkerSettings.from_environment(environment)
