from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import knowledge_agent.sync_runtime as sync_runtime
from function_app import app, sync_articles

FUNCTIONS_ROOT = Path(__file__).parents[1]
REPOSITORY_ROOT = Path(__file__).parents[3]


def test_function_app_entrypoint_is_discoverable() -> None:
    assert app is not None


def test_daily_timer_contract_and_past_due_reconcile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    functions = app.get_functions()
    assert [function.get_function_name() for function in functions] == ["sync_articles"]
    binding = functions[0].get_bindings()[0].get_dict_repr()
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


def test_queue_worker_is_configured_for_serial_processing() -> None:
    host = json.loads((FUNCTIONS_ROOT / "host.json").read_text(encoding="utf-8"))
    assert host["telemetryMode"] == "OpenTelemetry"
    assert host["extensions"]["queues"]["batchSize"] == 1
    assert host["extensions"]["queues"]["newBatchThreshold"] == 0


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
