"""Azure SDK adapters for the synchronization vertical slice.

SDK clients are injected so unit tests never contact Azure. Client construction lives
in ``sync_runtime`` and uses one ``DefaultAzureCredential`` instance per warm process.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from knowledge_agent.contracts import (
    CORPUS_ID,
    EMBEDDING_DIMENSIONS,
    SYNC_PARTITION,
    SYNC_ROW_KEY,
    CosmosChunk,
    SyncStateEntity,
)
from knowledge_agent.sync import IndexedArticle

_GIT_SHA_LENGTH = 40
_COSMOS_BATCH_LIMIT = 100
_COSMOS_BATCH_PAYLOAD_LIMIT = int(1.8 * 1024 * 1024)
_COSMOS_BATCH_ENVELOPE_BYTES = 1024
_COSMOS_OPERATION_OVERHEAD_BYTES = 256


class AdapterContractError(RuntimeError):
    """Raised when an SDK response cannot be mapped to the application contract."""


class ExternalServiceError(RuntimeError):
    """Sanitized Azure/Foundry failure that is safe for Function host logs."""


@dataclass(frozen=True, slots=True)
class SyncState:
    last_successful_sha: str | None
    last_run_at: str
    last_run_result: str


class TableSyncStateStore:
    def __init__(
        self,
        table_client: Any,
        *,
        not_found_error: type[Exception],
    ) -> None:
        self._table = table_client
        self._not_found_error = not_found_error

    def get(self) -> SyncState | None:
        try:
            entity = self._table.get_entity(
                partition_key=SYNC_PARTITION,
                row_key=SYNC_ROW_KEY,
            )
        except self._not_found_error:
            return None
        except Exception:
            raise ExternalServiceError("Table state read failed") from None
        if not isinstance(entity, Mapping):
            raise AdapterContractError("Table sync state must be an object")
        state = SyncStateEntity(
            last_successful_sha=entity.get("lastSuccessfulSha"),
            last_run_at=entity.get("lastRunAt"),
            last_run_result=entity.get("lastRunResult"),
        )
        state.to_entity()
        return SyncState(
            last_successful_sha=state.last_successful_sha,
            last_run_at=state.last_run_at,
            last_run_result=state.last_run_result,
        )

    def put(self, state: SyncStateEntity) -> None:
        try:
            self._table.upsert_entity(entity=state.to_entity(), mode="replace")
        except Exception:
            raise ExternalServiceError("Table state write failed") from None


def _required_string(document: Mapping[str, Any], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"Cosmos manifest field {field} is invalid")
    return value


def _required_sha(document: Mapping[str, Any], field: str) -> str:
    value = _required_string(document, field)
    if len(value) != _GIT_SHA_LENGTH or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise AdapterContractError(f"Cosmos manifest field {field} is invalid")
    return value


def _serialized_operation_size(operation: tuple[Any, ...]) -> int:
    operation_name, arguments = operation[0], operation[1]
    if operation_name == "upsert":
        serialized = json.dumps(
            arguments[0],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    elif operation_name == "delete":
        serialized = json.dumps(
            {"id": arguments[0]},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    else:
        raise AdapterContractError("unsupported Cosmos batch operation")
    return len(serialized) + _COSMOS_OPERATION_OVERHEAD_BYTES


def _partition_batch_operations(
    operations: Sequence[tuple[Any, ...]],
) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    batches: list[tuple[tuple[Any, ...], ...]] = []
    current: list[tuple[Any, ...]] = []
    current_size = _COSMOS_BATCH_ENVELOPE_BYTES
    for operation in operations:
        operation_size = _serialized_operation_size(operation)
        if operation_size + _COSMOS_BATCH_ENVELOPE_BYTES > _COSMOS_BATCH_PAYLOAD_LIMIT:
            raise AdapterContractError("one Cosmos batch operation exceeds the safe size limit")
        if (
            len(current) == _COSMOS_BATCH_LIMIT
            or current_size + operation_size > _COSMOS_BATCH_PAYLOAD_LIMIT
        ):
            batches.append(tuple(current))
            current = []
            current_size = _COSMOS_BATCH_ENVELOPE_BYTES
        current.append(operation)
        current_size += operation_size
    if current:
        batches.append(tuple(current))
    return tuple(batches)


class CosmosIndexRepository:
    def __init__(self, container_client: Any) -> None:
        self._container = container_client

    def list_articles(self) -> tuple[IndexedArticle, ...]:
        query = (
            "SELECT c.id, c.corpusId, c.articleId, c.chunkIndex, c.sourcePath, "
            "c.sourceRevision, c.sourceBlobSha, c.chunkingVersion FROM c "
            "WHERE c.corpusId = @corpusId"
        )
        try:
            documents = tuple(
                self._container.query_items(
                    query=query,
                    parameters=[{"name": "@corpusId", "value": CORPUS_ID}],
                    partition_key=CORPUS_ID,
                )
            )
        except Exception:
            raise ExternalServiceError("Cosmos manifest read failed") from None
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for document in documents:
            if not isinstance(document, Mapping):
                raise AdapterContractError("Cosmos manifest item must be an object")
            article_id = _required_string(document, "articleId")
            grouped[article_id].append(document)

        articles: list[IndexedArticle] = []
        for article_id, chunks in sorted(grouped.items()):
            normalized: list[tuple[int, str, str, str, str, str]] = []
            for chunk in chunks:
                chunk_index = chunk.get("chunkIndex")
                if type(chunk_index) is not int or chunk_index < 0:
                    raise AdapterContractError("Cosmos manifest chunkIndex is invalid")
                if _required_string(chunk, "corpusId") != CORPUS_ID:
                    raise AdapterContractError("Cosmos manifest corpusId is invalid")
                normalized.append(
                    (
                        chunk_index,
                        _required_string(chunk, "id"),
                        _required_string(chunk, "sourcePath"),
                        _required_sha(chunk, "sourceRevision"),
                        _required_sha(chunk, "sourceBlobSha"),
                        _required_string(chunk, "chunkingVersion"),
                    )
                )
            normalized.sort(key=lambda item: item[0])
            first = normalized[0]
            expected_indices = list(range(len(normalized)))
            actual_indices = [item[0] for item in normalized]
            metadata = {(item[2], item[3], item[4], item[5]) for item in normalized}
            ids_match = all(item[1] == f"{article_id}:{item[0]}" for item in normalized)
            needs_reindex = (
                actual_indices != expected_indices or len(metadata) != 1 or not ids_match
            )
            articles.append(
                IndexedArticle(
                    article_id=article_id,
                    source_path=first[2],
                    source_revision=first[3],
                    source_blob_sha=first[4],
                    chunking_version=first[5],
                    needs_reindex=needs_reindex,
                )
            )
        return tuple(articles)

    def _existing_ids(self, article_id: str) -> set[str]:
        try:
            documents = tuple(
                self._container.query_items(
                    query=(
                        "SELECT c.id FROM c WHERE c.corpusId = @corpusId "
                        "AND c.articleId = @articleId"
                    ),
                    parameters=[
                        {"name": "@corpusId", "value": CORPUS_ID},
                        {"name": "@articleId", "value": article_id},
                    ],
            partition_key=CORPUS_ID,
                )
            )
        except Exception:
            raise ExternalServiceError("Cosmos article read failed") from None
        ids: set[str] = set()
        for document in documents:
            if not isinstance(document, Mapping):
                raise AdapterContractError("Cosmos ID query item must be an object")
            item_id = _required_string(document, "id")
            if item_id in ids:
                raise AdapterContractError("Cosmos ID query returned a duplicate")
            ids.add(item_id)
        return ids

    def _execute_batches(self, operations: Sequence[tuple[Any, ...]]) -> None:
        for batch in _partition_batch_operations(operations):
            try:
                self._container.execute_item_batch(
                    batch_operations=list(batch),
                    partition_key=CORPUS_ID,
                )
            except Exception:
                raise ExternalServiceError("Cosmos article write failed") from None

    def replace_article(self, article_id: str, chunks: tuple[CosmosChunk, ...]) -> None:
        if not chunks or any(chunk.article_id != article_id for chunk in chunks):
            raise AdapterContractError("replacement chunks must belong to one article")
        if [chunk.chunk_index for chunk in chunks] != list(range(len(chunks))):
            raise AdapterContractError("replacement chunks must use contiguous indices")

        existing_ids = self._existing_ids(article_id)
        new_ids = {chunk.id for chunk in chunks}
        upserts = [("upsert", (chunk.to_document(),)) for chunk in reversed(chunks)]
        stale_deletes = [("delete", (item_id,)) for item_id in sorted(existing_ids - new_ids)]
        self._execute_batches((*upserts, *stale_deletes))

    def delete_article(self, article_id: str) -> None:
        deletes = [("delete", (item_id,)) for item_id in sorted(self._existing_ids(article_id))]
        self._execute_batches(deletes)


class FoundryEmbeddingProvider:
    def __init__(self, openai_client: Any, deployment_name: str) -> None:
        if not isinstance(deployment_name, str) or not deployment_name.strip():
            raise ValueError("embedding deployment name must not be empty")
        self._client = openai_client
        self._deployment_name = deployment_name

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        try:
            response = self._client.embeddings.create(
                model=self._deployment_name,
                input=list(texts),
                dimensions=EMBEDDING_DIMENSIONS,
            )
        except Exception:
            raise ExternalServiceError("Foundry embedding request failed") from None
        raw_data = getattr(response, "data", None)
        if not isinstance(raw_data, Sequence) or isinstance(raw_data, (str, bytes)):
            raise AdapterContractError("Foundry embedding response data is invalid")
        ordered: dict[int, tuple[float, ...]] = {}
        for item in raw_data:
            index = getattr(item, "index", None)
            embedding = getattr(item, "embedding", None)
            if type(index) is not int or index in ordered or not isinstance(embedding, Sequence):
                raise AdapterContractError("Foundry embedding response item is invalid")
            values = tuple(embedding)
            if (
                len(values) != EMBEDDING_DIMENSIONS
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in values
                )
            ):
                raise AdapterContractError("Foundry embedding vector is invalid")
            ordered[index] = values
        if set(ordered) != set(range(len(texts))):
            raise AdapterContractError("Foundry embedding response count is invalid")
        return tuple(ordered[index] for index in range(len(texts)))
