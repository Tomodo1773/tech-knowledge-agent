from __future__ import annotations

import pytest

from settings import AgentSettings

FOUNDRY_TEST_HOST = "account." + "services.ai.azure.com"
COSMOS_TEST_HOST = "knowledge." + "documents.azure.com"
VALID = {
    "FOUNDRY_PROJECT_ENDPOINT": f"https://{FOUNDRY_TEST_HOST}/api/projects/knowledge",
    "COSMOS_ENDPOINT": f"https://{COSMOS_TEST_HOST}/",
    "AZURE_AI_MODEL_DEPLOYMENT_NAME": "chat-model",
    "EMBEDDING_MODEL_DEPLOYMENT_NAME": "embedding-model",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
}


def test_reads_separate_chat_and_embedding_deployments() -> None:
    settings = AgentSettings.from_environment(VALID)

    assert settings.chat_deployment_name == "chat-model"
    assert settings.embedding_deployment_name == "embedding-model"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (
            "FOUNDRY_PROJECT_ENDPOINT",
            f"http://{FOUNDRY_TEST_HOST}/api/projects/x",
        ),
        ("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/api/projects/x"),
        ("COSMOS_ENDPOINT", f"https://{COSMOS_TEST_HOST}/dbs/knowledge"),
        ("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"),
    ],
)
def test_rejects_invalid_or_unsafe_settings(name: str, value: str) -> None:
    environment = {**VALID, name: value}

    with pytest.raises(RuntimeError):
        AgentSettings.from_environment(environment)


def test_rejects_missing_dedicated_embedding_deployment() -> None:
    environment = dict(VALID)
    del environment["EMBEDDING_MODEL_DEPLOYMENT_NAME"]

    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_DEPLOYMENT_NAME"):
        AgentSettings.from_environment(environment)
