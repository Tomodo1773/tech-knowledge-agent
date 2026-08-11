"""Synchronization handler over injected source, state, embedding, and index ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from opentelemetry.trace import Span

from knowledge_agent.contracts import SyncStateEntity
from knowledge_agent.sync import (
    EmbeddingProvider,
    IndexRepository,
    SyncResult,
    SyncSource,
    synchronize,
)
from knowledge_agent.telemetry import SPAN_SYNC_RUN, set_attributes, traced


class SyncStateStore(Protocol):
    def get(self) -> object | None: ...

    def put(self, state: SyncStateEntity) -> None: ...


class SyncRunFailed(RuntimeError):
    """Raised after a safely recorded synchronization failure."""


def _utc_timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("sync clock must return a UTC datetime")
    return value.isoformat().replace("+00:00", "Z")


def run_sync(
    *,
    source: SyncSource,
    index: IndexRepository,
    embedder: EmbeddingProvider,
    state_store: SyncStateStore,
    chunking_version: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SyncResult:
    with traced(SPAN_SYNC_RUN) as span:
        previous = state_store.get()
        previous_sha = getattr(previous, "last_successful_sha", None)
        timestamp = _utc_timestamp(now)
        try:
            result = synchronize(
                source=source,
                index=index,
                embedder=embedder,
                chunking_version=chunking_version,
                indexed_at=timestamp,
                last_successful_sha=previous_sha,
            )
        except Exception:
            _record(state_store, span, previous_sha, timestamp, "failed")
            raise

        if result.aborted:
            _record(state_store, span, previous_sha, timestamp, "failed")
            raise SyncRunFailed("synchronization aborted because an indexed article is invalid")

        _record(
            state_store,
            span,
            result.revision,
            timestamp,
            "partial" if result.errors else "success",
            error_count=len(result.errors),
        )
        return result


def _record(
    state_store: SyncStateStore,
    span: Span,
    revision: str | None,
    timestamp: str,
    run_result: str,
    *,
    error_count: int = 0,
) -> None:
    set_attributes(
        span,
        **{
            "knowledge.run_result": run_result,
            "knowledge.commit_sha": revision,
            "knowledge.error_count": error_count,
        },
    )
    state_store.put(
        SyncStateEntity(
            last_successful_sha=revision,
            last_run_at=timestamp,
            last_run_result=run_result,
        )
    )
