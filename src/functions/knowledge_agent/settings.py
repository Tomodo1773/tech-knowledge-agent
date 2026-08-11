"""Validated, service-specific environment settings.

Settings are split by request path so the Slack HTTP trigger does not need to load
Cosmos or Foundry configuration during a cold start. Secret values are excluded from
dataclass representations and are never included in validation messages.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from knowledge_agent.contracts import SettingName

_STORAGE_ACCOUNT_PATTERN = re.compile(r"^[a-z0-9]{3,24}$")
_GITHUB_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


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

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SyncSettings:
        owner = _required(environment, SettingName.GITHUB_OWNER)
        repository = _required(environment, SettingName.GITHUB_REPOSITORY)
        if not _GITHUB_IDENTIFIER_PATTERN.fullmatch(owner):
            raise SettingsError("GITHUB_OWNER is invalid")
        if not _GITHUB_IDENTIFIER_PATTERN.fullmatch(repository):
            raise SettingsError("GITHUB_REPOSITORY is invalid")
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
        )
