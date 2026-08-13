from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import knowledge_agent.sync_runtime as sync_runtime
from function_app import app, sync_articles

FUNCTIONS_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = Path(__file__).parents[3]


def test_function_app_entrypoint_is_discoverable() -> None:
    assert app is not None


# get_functions() rejects a second call, so the registration is read once per session.
_FUNCTIONS = app.get_functions()
REGISTERED = {
    function.get_function_name(): function.get_bindings()[0].get_dict_repr()
    for function in _FUNCTIONS
}


def _binding(name: str) -> dict:
    return REGISTERED[name]


def test_daily_timer_contract_and_past_due_reconcile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert list(REGISTERED) == ["sync_articles", "slack_events", "agent_worker"]
    binding = _binding("sync_articles")
    assert binding["type"] == "timerTrigger"
    assert binding["schedule"] == "0 0 18 * * *"
    assert binding["runOnStartup"] is False
    assert binding["useMonitor"] is True

    calls = 0

    def run() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(sync_runtime, "run_configured_sync", run)
    sync_articles(SimpleNamespace(past_due=True))  # type: ignore[arg-type]
    assert calls == 1


def test_slack_request_url_is_anonymous_and_post_only() -> None:
    binding = _binding("slack_events")
    assert binding["type"] == "httpTrigger"
    assert binding["route"] == "slack/events"
    assert [str(method.value) for method in binding["methods"]] == ["POST"]
    assert binding["authLevel"].value == "anonymous"


def test_every_trigger_binding_name_matches_its_python_parameter() -> None:
    # The worker refuses to load a function whose trigger binding name is not also a
    # declared parameter, and it only reports this at host startup, so a mismatch passes
    # every handler-level test and then crashes the deployed app. @app.route defaults the
    # binding name to "req", which does not match a handler that names the parameter
    # differently, so arg_name has to be explicit on each trigger.
    for function in _FUNCTIONS:
        binding_name = function.get_bindings()[0].get_dict_repr()["name"]
        parameters = inspect.signature(function.get_user_function()).parameters
        assert binding_name in parameters, (
            f"{function.get_function_name()} binds {binding_name!r} "
            f"but declares {sorted(parameters)!r}"
        )


def test_queue_worker_is_configured_for_serial_processing() -> None:
    binding = _binding("agent_worker")
    assert binding["type"] == "queueTrigger"
    assert binding["queueName"] == "slack-questions"
    assert binding["connection"] == "AzureWebJobsStorage"

    host = json.loads((FUNCTIONS_ROOT / "host.json").read_text(encoding="utf-8"))
    assert host["telemetryMode"] == "OpenTelemetry"
    # host.json filters reach the host process only, and its Information logs are the
    # option dumps it prints on every instance start. Invocation outcomes live in the
    # requests table, so exporting them as logs adds nothing. Scoped to the
    # OpenTelemetry provider so console logging keeps its level.
    assert host["logging"]["OpenTelemetry"]["logLevel"]["default"] == "Warning"
    assert host["extensions"]["queues"]["batchSize"] == 1
    assert host["extensions"]["queues"]["newBatchThreshold"] == 0
    # The producer base64-encodes, so the trigger must not fall back to plain text.
    assert host["extensions"]["queues"]["messageEncoding"] == "base64"


def test_trigger_registration_does_not_load_the_heavy_sdks() -> None:
    """A Slack event must not pay the cold-start cost of Cosmos, Foundry, or the Agent."""
    probe = (
        "import sys, json;"
        "import function_app;"
        "import knowledge_agent.slack_runtime;"
        "print(json.dumps(sorted(name for name in sys.modules"
        " if name in {'azure.cosmos', 'azure.ai.projects', 'openai', 'azure.data.tables'})))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=FUNCTIONS_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_local_settings_and_dotenv_files_are_excluded_from_package() -> None:
    patterns = (FUNCTIONS_ROOT / ".funcignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in patterns
    assert ".env.*" in patterns
    assert "local.settings.json" in patterns


def test_application_insights_uses_managed_identity_without_local_auth_key() -> None:
    functions_bicep = (REPOSITORY_ROOT / "infra/app/functions.bicep").read_text(
        encoding="utf-8"
    )
    foundry_bicep = (REPOSITORY_ROOT / "infra/app/foundry.bicep").read_text(
        encoding="utf-8"
    )
    publisher_role_id = "3913510d-42f4-4e42-8a64-420c390055eb"

    assert "APPLICATIONINSIGHTS_AUTHENTICATION_STRING" in functions_bicep
    assert "Authorization=AAD;ClientId=${functionIdentityClientId}" in functions_bicep
    assert publisher_role_id in functions_bicep
    assert "authType: 'ProjectManagedIdentity'" in foundry_bicep
    assert "ApplicationInsightsConnectionString" in foundry_bicep
    assert publisher_role_id in foundry_bicep
    assert "authType: 'ApiKey'" not in foundry_bicep
    assert "credentials:" not in foundry_bicep
