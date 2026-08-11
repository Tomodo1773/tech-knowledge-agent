"""Azure Functions v2 entry point with lazy SDK loading per trigger path."""

import logging

import azure.functions as func

from knowledge_agent.contracts import SLACK_QUEUE_NAME

app = func.FunctionApp()


@app.timer_trigger(
    schedule="0 0 18 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def sync_articles(timer: func.TimerRequest) -> None:
    """Reconcile the complete article manifest, including past-due invocations."""
    from knowledge_agent.sync_runtime import run_configured_sync

    run_configured_sync()


@app.route(
    route="slack/events",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def slack_events(request: func.HttpRequest) -> func.HttpResponse:
    """Accept Slack events. The Request URL carries no key; signature and allowlist guard it."""
    from knowledge_agent.slack_runtime import handle_configured_slack_request

    result = handle_configured_slack_request(
        raw_body=request.get_body(),
        timestamp_header=request.headers.get("X-Slack-Request-Timestamp"),
        signature_header=request.headers.get("X-Slack-Signature"),
    )
    logging.info("slack_events outcome=%s", result.audit_reason)
    return func.HttpResponse(
        result.body,
        status_code=result.status_code,
        mimetype="text/plain",
    )


@app.queue_trigger(
    arg_name="message",
    queue_name=SLACK_QUEUE_NAME,
    connection="AzureWebJobsStorage",
)
def agent_worker(message: func.QueueMessage) -> None:
    """Answer one queued question. Host retries and the poison queue handle failures."""
    from knowledge_agent.slack_runtime import run_configured_worker

    run_configured_worker(message.get_body())
