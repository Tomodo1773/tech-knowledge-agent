"""Fail-closed Hosted Agent runtime settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Required setting is missing: {name}")
    return value.strip()


def _https_endpoint(value: str, *, suffix: str, project_path: bool) -> str:
    parsed = urlparse(value)
    valid_path = (
        parsed.path.startswith("/api/projects/")
        if project_path
        else parsed.path in ("", "/")
    )
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(suffix)
        or parsed.username is not None
        or parsed.password is not None
        # Cosmos hands out its endpoint with an explicit :443, so rejecting every port
        # rejects the real value. Only a non-HTTPS port is a redirection attempt.
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not valid_path
    ):
        raise RuntimeError("Hosted Agent endpoint setting is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AgentSettings:
    foundry_project_endpoint: str
    cosmos_endpoint: str
    chat_deployment_name: str
    embedding_deployment_name: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> AgentSettings:
        capture = _required(environment, "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
        if capture.lower() != "true":
            raise RuntimeError("GenAI message-content telemetry must be enabled")
        return cls(
            foundry_project_endpoint=_https_endpoint(
                _required(environment, "FOUNDRY_PROJECT_ENDPOINT"),
                suffix=".services.ai.azure.com",
                project_path=True,
            ),
            cosmos_endpoint=_https_endpoint(
                _required(environment, "COSMOS_ENDPOINT"),
                suffix=".documents.azure.com",
                project_path=False,
            ),
            chat_deployment_name=_required(environment, "AZURE_AI_MODEL_DEPLOYMENT_NAME"),
            embedding_deployment_name=_required(
                environment, "EMBEDDING_MODEL_DEPLOYMENT_NAME"
            ),
        )
