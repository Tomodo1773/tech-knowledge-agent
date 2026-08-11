"""Synchronization handler over injected source, state, embedding, and index ports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from knowledge_agent.contracts import SyncStateEntity
from knowledge_agent.sync import (
    EmbeddingProvider,
    IndexRepository,
    SyncResult,
    SyncSource,
    synchronize,
)


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
        state_store.put(
            SyncStateEntity(
                last_successful_sha=previous_sha,
                last_run_at=timestamp,
                last_run_result="failed",
            )
        )
        raise

    if result.aborted:
        state_store.put(
            SyncStateEntity(
                last_successful_sha=previous_sha,
                last_run_at=timestamp,
                last_run_result="failed",
            )
        )
        raise SyncRunFailed("synchronization aborted because an indexed article is invalid")

    state_store.put(
        SyncStateEntity(
            last_successful_sha=result.revision,
            last_run_at=timestamp,
            last_run_result="partial" if result.errors else "success",
        )
    )
    return result
