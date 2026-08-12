"""Wire the azd-generated Hosted Agent Responses endpoint into the Function App."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit

AGENT_ENDPOINT_ENV = "AGENT_KNOWLEDGE_AGENT_RESPONSES_ENDPOINT"
FUNCTION_APP_ENV = "SERVICE_FUNCTIONS_RESOURCE_NAME"
RESOURCE_GROUP_ENV = "AZURE_RESOURCE_GROUP"
SUBSCRIPTION_ENV = "AZURE_SUBSCRIPTION_ID"
FUNCTION_SETTING = "KNOWLEDGE_AGENT_ENDPOINT"

_RESPONSES_PATH = re.compile(
    r"^/api/projects/[^/]+/agents/[^/]+/endpoint/protocols/openai/responses/?$"
)
_FOUNDRY_HOST = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.services\.ai\.azure\.com$"
)
_SUBSCRIPTION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class WiringError(RuntimeError):
    """Raised when endpoint wiring cannot proceed safely."""


def _required_value(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value or value != value.strip() or any(ord(character) < 32 for character in value):
        raise WiringError(f"required azd environment value {name} is missing or invalid")
    return value


def validate_agent_endpoint(value: str) -> str:
    """Return a validated public-cloud Foundry Responses endpoint."""
    if not value or value != value.strip() or len(value) > 2048:
        raise WiringError("the azd-generated Agent Responses endpoint is empty or invalid")
    if any(ord(character) < 32 for character in value):
        raise WiringError("the azd-generated Agent Responses endpoint is empty or invalid")

    try:
        endpoint = urlsplit(value)
        port = endpoint.port
    except ValueError as error:
        raise WiringError("the azd-generated Agent Responses endpoint is malformed") from error

    host = (endpoint.hostname or "").lower()
    if (
        endpoint.scheme.lower() != "https"
        or endpoint.username is not None
        or endpoint.password is not None
        or port not in (None, 443)
        or not _FOUNDRY_HOST.fullmatch(host)
        or not _RESPONSES_PATH.fullmatch(endpoint.path)
        or endpoint.fragment
    ):
        raise WiringError("the azd-generated value is not a Foundry Responses endpoint")

    query = parse_qsl(endpoint.query, keep_blank_values=True)
    if any(key != "api-version" or not query_value for key, query_value in query):
        raise WiringError("the Agent Responses endpoint has unsupported query parameters")
    return value


def validate_subscription_id(value: str) -> str:
    """Return a canonical Azure subscription UUID without using CLI current context."""
    if not _SUBSCRIPTION_ID.fullmatch(value) or int(value.replace("-", ""), 16) == 0:
        raise WiringError("AZURE_SUBSCRIPTION_ID is missing or invalid")
    return value.lower()


def _run_az(runner: Runner, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # subprocess without shell=True does not apply PATHEXT, so a bare "az" never
    # resolves to az.cmd on Windows. shutil.which does apply PATHEXT and finds it.
    az_executable = shutil.which("az") or "az"
    return runner(
        [az_executable, *arguments, "--only-show-errors"],
        capture_output=True,
        check=False,
        text=True,
    )


def wire_agent_endpoint(
    *,
    endpoint: str,
    function_app_name: str,
    resource_group: str,
    subscription_id: str,
    runner: Runner = subprocess.run,
) -> bool:
    """Set the Function App setting when its current value differs."""
    endpoint = validate_agent_endpoint(endpoint)
    subscription_id = validate_subscription_id(subscription_id)
    for name, value in (
        (FUNCTION_APP_ENV, function_app_name),
        (RESOURCE_GROUP_ENV, resource_group),
    ):
        _required_value({name: value}, name)

    current = _run_az(
        runner,
        [
            "functionapp",
            "config",
            "appsettings",
            "list",
            "--name",
            function_app_name,
            "--resource-group",
            resource_group,
            "--subscription",
            subscription_id,
            "--query",
            f"[?name=='{FUNCTION_SETTING}'].value | [0]",
            "--output",
            "tsv",
        ],
    )
    if current.returncode != 0:
        raise WiringError("Azure CLI could not read the Function App setting")
    if current.stdout.rstrip("\r\n") == endpoint:
        return False

    updated = _run_az(
        runner,
        [
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--name",
            function_app_name,
            "--resource-group",
            resource_group,
            "--subscription",
            subscription_id,
            "--settings",
            f"{FUNCTION_SETTING}={endpoint}",
            "--output",
            "none",
        ],
    )
    if updated.returncode != 0:
        raise WiringError("Azure CLI could not update the Function App setting")
    return True


def main(environment: Mapping[str, str] = os.environ, runner: Runner = subprocess.run) -> int:
    try:
        changed = wire_agent_endpoint(
            endpoint=_required_value(environment, AGENT_ENDPOINT_ENV),
            function_app_name=_required_value(environment, FUNCTION_APP_ENV),
            resource_group=_required_value(environment, RESOURCE_GROUP_ENV),
            subscription_id=validate_subscription_id(
                _required_value(environment, SUBSCRIPTION_ENV)
            ),
            runner=runner,
        )
    except WiringError as error:
        print(f"Agent endpoint wiring failed: {error}", file=sys.stderr)
        return 1

    state = "updated" if changed else "already current"
    print(f"{FUNCTION_SETTING} wiring is {state}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
