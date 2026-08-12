from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).parents[3] / "scripts" / "set_agent_endpoint.py"
SPEC = importlib.util.spec_from_file_location("set_agent_endpoint", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
wiring = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wiring)

FOUNDRY_TEST_HOST = "account." + "services.ai.azure.com"
VALID_ENDPOINT = (
    f"https://{FOUNDRY_TEST_HOST}/api/projects/project/agents/knowledge-agent/"
    "endpoint/protocols/openai/responses"
)
VALID_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
ROOT = Path(__file__).parents[3]
ROLE_SCRIPT = ROOT / "scripts" / "assign-agent-roles.ps1"


class FakeRunner:
    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self.results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        return self.results.pop(0)


def result(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        " http://account.services.ai.azure.com/api/projects/p/agents/a/endpoint/protocols/"
        "openai/responses",
        "https://example.com/api/projects/p/agents/a/endpoint/protocols/openai/responses",
        f"https://{FOUNDRY_TEST_HOST}/api/projects/p/agents/a/versions/1",
        f"{VALID_ENDPOINT}#fragment",
        f"{VALID_ENDPOINT}?token=secret",
    ],
)
def test_invalid_endpoint_is_rejected_before_azure_cli(endpoint: str) -> None:
    runner = FakeRunner()

    with pytest.raises(wiring.WiringError):
        wiring.wire_agent_endpoint(
            endpoint=endpoint,
            function_app_name="func-test",
            resource_group="rg-test",
            subscription_id=VALID_SUBSCRIPTION_ID,
            runner=runner,
        )

    assert runner.calls == []


def test_api_version_query_is_allowed() -> None:
    endpoint = f"{VALID_ENDPOINT}?api-version=v1"
    runner = FakeRunner(result(stdout=f"{endpoint}\n"))

    assert not wiring.wire_agent_endpoint(
        endpoint=endpoint,
        function_app_name="func-test",
        resource_group="rg-test",
        subscription_id=VALID_SUBSCRIPTION_ID,
        runner=runner,
    )


def test_current_value_is_a_no_op_and_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(result(stdout=f"{VALID_ENDPOINT}\n"))
    environment = {
        wiring.AGENT_ENDPOINT_ENV: VALID_ENDPOINT,
        wiring.FUNCTION_APP_ENV: "func-test",
        wiring.RESOURCE_GROUP_ENV: "rg-test",
        wiring.SUBSCRIPTION_ENV: VALID_SUBSCRIPTION_ID,
    }

    assert wiring.main(environment, runner) == 0

    captured = capsys.readouterr()
    assert VALID_ENDPOINT not in captured.out
    assert VALID_ENDPOINT not in captured.err
    assert len(runner.calls) == 1


def test_changed_value_is_set_without_cli_output(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(result(stdout=""), result(stdout=VALID_ENDPOINT))
    environment = {
        wiring.AGENT_ENDPOINT_ENV: VALID_ENDPOINT,
        wiring.FUNCTION_APP_ENV: "func-test",
        wiring.RESOURCE_GROUP_ENV: "rg-test",
        wiring.SUBSCRIPTION_ENV: VALID_SUBSCRIPTION_ID,
    }

    assert wiring.main(environment, runner) == 0

    captured = capsys.readouterr()
    assert VALID_ENDPOINT not in captured.out
    assert VALID_ENDPOINT not in captured.err
    assert len(runner.calls) == 2
    update = runner.calls[1]
    assert f"{wiring.FUNCTION_SETTING}={VALID_ENDPOINT}" in update
    assert update[update.index("--output") + 1] == "none"
    assert all(
        call[call.index("--subscription") + 1] == VALID_SUBSCRIPTION_ID
        for call in runner.calls
    )


def test_missing_generated_endpoint_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()

    assert wiring.main({}, runner) == 1

    captured = capsys.readouterr()
    assert wiring.AGENT_ENDPOINT_ENV in captured.err
    assert runner.calls == []


def test_cli_failure_does_not_echo_endpoint(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(result(returncode=1, stderr=f"failed for {VALID_ENDPOINT}"))
    environment = {
        wiring.AGENT_ENDPOINT_ENV: VALID_ENDPOINT,
        wiring.FUNCTION_APP_ENV: "func-test",
        wiring.RESOURCE_GROUP_ENV: "rg-test",
        wiring.SUBSCRIPTION_ENV: VALID_SUBSCRIPTION_ID,
    }

    assert wiring.main(environment, runner) == 1

    captured = capsys.readouterr()
    assert VALID_ENDPOINT not in captured.out
    assert VALID_ENDPOINT not in captured.err


@pytest.mark.parametrize(
    "subscription_id", ["", "not-a-uuid", "00000000-0000-0000-0000-000000000000"]
)
def test_invalid_subscription_fails_before_azure_cli(subscription_id: str) -> None:
    runner = FakeRunner()
    environment = {
        wiring.AGENT_ENDPOINT_ENV: VALID_ENDPOINT,
        wiring.FUNCTION_APP_ENV: "func-test",
        wiring.RESOURCE_GROUP_ENV: "rg-test",
        wiring.SUBSCRIPTION_ENV: subscription_id,
    }

    assert wiring.main(environment, runner) == 1
    assert runner.calls == []


def test_postdeploy_hook_uses_no_sync_python_on_windows_and_posix() -> None:
    azure_yaml = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    postdeploy = azure_yaml["hooks"]["postdeploy"]
    endpoint_command = (
        "uv run --project src/functions --no-sync python scripts/set_agent_endpoint.py"
    )
    principal = "instance_identity.principal_id"

    assert postdeploy["windows"]["shell"] == "pwsh"
    assert postdeploy["posix"]["shell"] == "sh"
    assert endpoint_command in postdeploy["windows"]["run"]
    assert endpoint_command in postdeploy["posix"]["run"]
    assert principal in postdeploy["windows"]["run"]
    assert principal in postdeploy["posix"]["run"]
    assert postdeploy["windows"]["run"].index("assign-agent-roles.ps1") < postdeploy[
        "windows"
    ]["run"].index("set_agent_endpoint.py")
    assert postdeploy["posix"]["run"].index("assign-agent-roles.ps1") < postdeploy[
        "posix"
    ]["run"].index("set_agent_endpoint.py")
    assert postdeploy["windows"]["continueOnError"] is False
    assert postdeploy["posix"]["continueOnError"] is False


def test_agent_receives_cosmos_endpoint_from_azd_environment() -> None:
    azure_yaml = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
    variables = {
        item["name"]: item["value"]
        for item in azure_yaml["services"]["knowledge-agent"]["environmentVariables"]
    }

    assert variables["COSMOS_ENDPOINT"] == "${COSMOS_ENDPOINT}"
    assert variables["EMBEDDING_MODEL_DEPLOYMENT_NAME"] == (
        "${EMBEDDING_MODEL_DEPLOYMENT_NAME}"
    )
    assert variables["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "true"
    assert "COSMOS_DATABASE_NAME" not in variables
    assert "COSMOS_CONTAINER_NAME" not in variables


def test_agent_role_script_rejects_invalid_principal_before_azure_cli() -> None:
    completed = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROLE_SCRIPT),
            "-SubscriptionId",
            VALID_SUBSCRIPTION_ID,
            "-ResourceGroupName",
            "rg-test",
            "-CosmosAccountName",
            "cosmos-test",
            "-AgentPrincipalId",
            "not-a-uuid",
            "-WhatIf",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "az cosmosdb" not in completed.stdout


def test_postdeploy_role_failure_does_not_leak_azure_cli_output() -> None:
    leaked = "leaked-role-" + "material-123"
    with tempfile.TemporaryDirectory(prefix=".test-hook-", dir=ROOT) as temp_directory:
        fake_bin = Path(temp_directory)
        fake_agent_json = (
            '{"instance_identity":{"principal_id":'
            '"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}}'
        )
        if os.name == "nt":
            fake_az = fake_bin / "az.cmd"
            fake_az.write_text(
                f"@echo off\necho {leaked}\necho {leaked} 1>&2\nexit /b 17\n",
                encoding="utf-8",
            )
            fake_azd = fake_bin / "azd.cmd"
            fake_azd.write_text(f"@echo off\necho {fake_agent_json}\n", encoding="utf-8")
            hook_name = "windows"
            command = ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
        else:
            fake_az = fake_bin / "az"
            fake_az.write_text(
                f"#!/bin/sh\necho '{leaked}'\necho '{leaked}' >&2\nexit 17\n",
                encoding="utf-8",
            )
            fake_az.chmod(0o700)
            fake_azd = fake_bin / "azd"
            fake_azd.write_text(f"#!/bin/sh\necho '{fake_agent_json}'\n", encoding="utf-8")
            fake_azd.chmod(0o700)
            hook_name = "posix"
            command = ["sh", "-c"]
        azure_yaml = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
        hook = azure_yaml["hooks"]["postdeploy"][hook_name]["run"]
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "AZURE_SUBSCRIPTION_ID": VALID_SUBSCRIPTION_ID,
            "AZURE_RESOURCE_GROUP": "rg-test",
            "COSMOS_ACCOUNT_NAME": "cosmos-test",
        }

        completed = subprocess.run(
            [*command, hook],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    combined = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert leaked not in combined
    assert "Hosted Agent Cosmos role assignment failed." in combined
