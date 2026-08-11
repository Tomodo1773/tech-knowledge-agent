"""Azure Functions v2 entry point with lazy SDK loading per trigger path."""

import azure.functions as func

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
