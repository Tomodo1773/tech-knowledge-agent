from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from knowledge_agent.contracts import SyncStateEntity
from knowledge_agent.sync import ArticleSyncError, SyncResult
from knowledge_agent.sync_function import SyncRunFailed, run_sync

REVISION = "0123456789abcdef0123456789abcdef01234567"
PREVIOUS = "f" * 40
NOW = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)


class FakeStateStore:
    def __init__(self, previous_sha: str | None = PREVIOUS, *, fail_put: bool = False) -> None:
        self.previous = SimpleNamespace(last_successful_sha=previous_sha)
        self.puts: list[SyncStateEntity] = []
        self.fail_put = fail_put

    def get(self) -> object:
        return self.previous

    def put(self, state: SyncStateEntity) -> None:
        if self.fail_put:
            raise RuntimeError("table unavailable")
        self.puts.append(state)


def _run(monkeypatch: pytest.MonkeyPatch, result: SyncResult, store: FakeStateStore) -> SyncResult:
    monkeypatch.setattr("knowledge_agent.sync_function.synchronize", lambda **_: result)
    return run_sync(
        source=object(),  # type: ignore[arg-type]
        index=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        state_store=store,
        chunking_version="v1",
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    ("errors", "expected_result"),
    [
        ((), "success"),
        ((ArticleSyncError("articles/bad.md", "invalid", False),), "partial"),
    ],
)
def test_success_and_new_invalid_partial_advance_head(
    monkeypatch: pytest.MonkeyPatch,
    errors: tuple[ArticleSyncError, ...],
    expected_result: str,
) -> None:
    store = FakeStateStore()
    result = SyncResult(revision=REVISION, unchanged=False, errors=errors)

    assert _run(monkeypatch, result, store) is result

    assert store.puts[0].last_successful_sha == REVISION
    assert store.puts[0].last_run_result == expected_result
    assert store.puts[0].last_run_at == "2026-08-11T18:00:00Z"


def test_existing_invalid_records_failed_keeps_sha_and_fails_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStateStore()
    result = SyncResult(
        revision=REVISION,
        unchanged=False,
        aborted=True,
        errors=(ArticleSyncError("articles/bad.md", "invalid", True),),
    )

    with pytest.raises(SyncRunFailed, match="indexed article"):
        _run(monkeypatch, result, store)

    assert store.puts[0].last_successful_sha == PREVIOUS
    assert store.puts[0].last_run_result == "failed"


def test_transport_failure_records_failed_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStateStore()

    def fail(**_: object) -> SyncResult:
        raise RuntimeError("transport unavailable")

    monkeypatch.setattr("knowledge_agent.sync_function.synchronize", fail)
    with pytest.raises(RuntimeError, match="transport unavailable"):
        run_sync(
            source=object(),  # type: ignore[arg-type]
            index=object(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
            state_store=store,
            chunking_version="v1",
            now=lambda: NOW,
        )

    assert store.puts[0].last_successful_sha == PREVIOUS
    assert store.puts[0].last_run_result == "failed"


def test_table_result_failure_fails_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStateStore(fail_put=True)
    result = SyncResult(revision=REVISION, unchanged=True)

    with pytest.raises(RuntimeError, match="table unavailable"):
        _run(monkeypatch, result, store)
