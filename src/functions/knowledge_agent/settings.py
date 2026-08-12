"""Validated, service-specific environment settings.

Settings are split by request path so the Slack HTTP trigger does not need to load
Cosmos or Foundry configuration during a cold start. Secret values are excluded from
dataclass representations and are never included in validation messages.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlparse

from knowledge_agent.contracts import SettingName

DEFAULT_AGENT_API_VERSION = "v1"

_STORAGE_ACCOUNT_PATTERN = re.compile(r"^[a-z0-9]{3,24}$")
_GITHUB_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_SLACK_ID_PATTERN = re.compile(r"^[A-Z0-9]{2,32}$")
_AGENT_RESPONSES_PATH = re.compile(
    r"^/api/projects/[^/]+/agents/[^/]+/endpoint/protocols/openai/responses/?$"
)
_SLACK_BOT_TOKEN_PATTERN = re.compile(r"^xoxb-[A-Za-z0-9-]{10,}$")
# Fine-grained and classic personal access tokens. The repository is private, so the
# sync cannot fall back to anonymous reads.
_GITHUB_TOKEN_PATTERN = re.compile(r"^(github_pat_[A-Za-z0-9_]{30,255}|ghp_[A-Za-z0-9]{36,255})$")


class SettingsError(ValueError):
    """Raised when required runtime configuration is missing or unsafe."""


def _required(environment: Mapping[str, str], name: SettingName) -> str:
    value = environment.get(name.value)
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"required setting {name.value} is missing")
    return value.strip()


def _https_endpoint(
    value: str,
    name: SettingName,
    *,
    suffix: str,
    path_pattern: re.Pattern[str],
) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(suffix)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or path_pattern.fullmatch(parsed.path) is None
    ):
        raise SettingsError(f"{name.value} must be an approved HTTPS endpoint")
    return value.rstrip("/")


def _storage_account(environment: Mapping[str, str]) -> str:
    value = _required(environment, SettingName.AZURE_STORAGE_ACCOUNT_NAME)
    if not _STORAGE_ACCOUNT_PATTERN.fullmatch(value):
        raise SettingsError("AZURE_STORAGE_ACCOUNT_NAME is invalid")
    return value


@dataclass(frozen=True, slots=True)
class SyncSettings:
    storage_account_name: str
    cosmos_endpoint: str
    foundry_project_endpoint: str
    embedding_deployment_name: str
    github_owner: str
    github_repository: str
    github_default_branch: str
    chunking_version: str
    github_token: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SyncSettings:
        owner = _required(environment, SettingName.GITHUB_OWNER)
        repository = _required(environment, SettingName.GITHUB_REPOSITORY)
        token = _required(environment, SettingName.GITHUB_TOKEN)
        if not _GITHUB_IDENTIFIER_PATTERN.fullmatch(owner):
            raise SettingsError("GITHUB_OWNER is invalid")
        if not _GITHUB_IDENTIFIER_PATTERN.fullmatch(repository):
            raise SettingsError("GITHUB_REPOSITORY is invalid")
        if not _GITHUB_TOKEN_PATTERN.fullmatch(token):
            raise SettingsError("GITHUB_TOKEN is not a personal access token")
        return cls(
            storage_account_name=_storage_account(environment),
            cosmos_endpoint=_https_endpoint(
                _required(environment, SettingName.COSMOS_ENDPOINT),
                SettingName.COSMOS_ENDPOINT,
                suffix=".documents.azure.com",
                path_pattern=re.compile(r"^/?$"),
            ),
            foundry_project_endpoint=_https_endpoint(
                _required(environment, SettingName.FOUNDRY_PROJECT_ENDPOINT),
                SettingName.FOUNDRY_PROJECT_ENDPOINT,
                suffix=".services.ai.azure.com",
                path_pattern=re.compile(r"^/api/projects/[^/]+/?$"),
            ),
            embedding_deployment_name=_required(
                environment, SettingName.EMBEDDING_MODEL_DEPLOYMENT_NAME
            ),
            github_owner=owner,
            github_repository=repository,
            github_default_branch=_required(environment, SettingName.GITHUB_DEFAULT_BRANCH),
            chunking_version=_required(environment, SettingName.CHUNKING_VERSION),
            github_token=token,
        )


@dataclass(frozen=True, slots=True)
class SlackEventsSettings:
    """Settings for the Slack HTTP trigger. The signing secret never reaches a log."""

    storage_account_name: str
    allowed_team_id: str
    allowed_user_id: str
    signing_secret: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SlackEventsSettings:
        team_id = _required(environment, SettingName.SLACK_ALLOWED_TEAM_ID)
        user_id = _required(environment, SettingName.SLACK_ALLOWED_USER_ID)
        if not _SLACK_ID_PATTERN.fullmatch(team_id):
            raise SettingsError("SLACK_ALLOWED_TEAM_ID is invalid")
        if not _SLACK_ID_PATTERN.fullmatch(user_id):
            raise SettingsError("SLACK_ALLOWED_USER_ID is invalid")
        signing_secret = _required(environment, SettingName.SLACK_SIGNING_SECRET)
        if len(signing_secret) < 16:
            raise SettingsError("SLACK_SIGNING_SECRET is invalid")
        return cls(
            storage_account_name=_storage_account(environment),
            allowed_team_id=team_id,
            allowed_user_id=user_id,
            signing_secret=signing_secret,
        )


@dataclass(frozen=True, slots=True)
class AgentEndpoint:
    """Split form of the Responses endpoint that the OpenAI client needs."""

    base_url: str
    api_version: str


def parse_agent_endpoint(value: str) -> AgentEndpoint:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise SettingsError("KNOWLEDGE_AGENT_ENDPOINT is not a valid URL") from None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host.endswith(".services.ai.azure.com")
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _AGENT_RESPONSES_PATH.fullmatch(parsed.path) is None
    ):
        raise SettingsError("KNOWLEDGE_AGENT_ENDPOINT is not a Foundry Responses endpoint")

    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key != "api-version" or not query_value for key, query_value in query):
        raise SettingsError("KNOWLEDGE_AGENT_ENDPOINT has unsupported query parameters")
    if len(query) > 1:
        raise SettingsError("KNOWLEDGE_AGENT_ENDPOINT repeats api-version")

    # The OpenAI client appends "/responses" to base_url, so hand it the protocol root.
    base_path = parsed.path.rstrip("/").removesuffix("/responses")
    return AgentEndpoint(
        base_url=f"https://{host}{base_path}",
        api_version=query[0][1] if query else DEFAULT_AGENT_API_VERSION,
    )


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Settings for the Queue trigger. The bot token never reaches a log."""

    storage_account_name: str
    agent_endpoint: AgentEndpoint
    bot_token: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> WorkerSettings:
        bot_token = _required(environment, SettingName.SLACK_BOT_TOKEN)
        if not _SLACK_BOT_TOKEN_PATTERN.fullmatch(bot_token):
            raise SettingsError("SLACK_BOT_TOKEN is invalid")
        return cls(
            storage_account_name=_storage_account(environment),
            agent_endpoint=parse_agent_endpoint(
                _required(environment, SettingName.KNOWLEDGE_AGENT_ENDPOINT)
            ),
            bot_token=bot_token,
        )
