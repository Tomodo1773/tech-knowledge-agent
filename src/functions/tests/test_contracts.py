from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

FUNCTIONS_ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(FUNCTIONS_ROOT))

from knowledge_agent.contracts import (  # noqa: E402
    CORPUS_ID,
    COSMOS_CONTAINER_NAME,
    COSMOS_DATABASE_NAME,
    EMBEDDING_DIMENSIONS,
    REQUIRED_SETTING_NAMES,
    SLACK_QUEUE_NAME,
    STATE_TABLE_NAME,
    ContractError,
    ConversationStateEntity,
    CosmosChunk,
    EventStateEntity,
    QueueMessage,
    SyncStateEntity,
    conversation_row_key,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "contracts.json"


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_queue_message_round_trip_matches_fixture(self) -> None:
        expected = self.fixture["queueMessage"]
        message = QueueMessage.from_dict(expected)
        self.assertEqual(message.to_dict(), expected)
        correlation_field = self.fixture["semantics"]["correlationIdField"]
        self.assertEqual(message.correlation_id, expected[correlation_field])

    def test_queue_message_rejects_secret_fields(self) -> None:
        message = dict(self.fixture["queueMessage"])
        message["signingSecret"] = "must-not-travel-in-queue"
        with self.assertRaisesRegex(ContractError, "unknown=.*signingSecret"):
            QueueMessage.from_dict(message)

    def test_table_entities_match_fixture(self) -> None:
        expected = self.fixture["tableEntities"]
        self.assertEqual(
            SyncStateEntity(
                last_successful_sha=expected["sync"]["lastSuccessfulSha"],
                last_run_at=expected["sync"]["lastRunAt"],
                last_run_result=expected["sync"]["lastRunResult"],
            ).to_entity(),
            expected["sync"],
        )
        self.assertEqual(
            SyncStateEntity(
                last_successful_sha=None,
                last_run_at=expected["syncInitialFailure"]["lastRunAt"],
                last_run_result=expected["syncInitialFailure"]["lastRunResult"],
            ).to_entity(),
            expected["syncInitialFailure"],
        )
        self.assertEqual(
            EventStateEntity(
                event_id=expected["event"]["RowKey"],
                received_at=expected["event"]["receivedAt"],
            ).to_entity(),
            expected["event"],
        )
        self.assertEqual(
            ConversationStateEntity(
                thread_key_hash=expected["conversation"]["RowKey"],
                response_id=expected["conversation"]["responseId"],
                updated_at=expected["conversation"]["updatedAt"],
            ).to_entity(),
            expected["conversation"],
        )

    def test_sync_state_rejects_unknown_result(self) -> None:
        with self.assertRaisesRegex(ContractError, "success, partial, or failed"):
            SyncStateEntity(
                last_successful_sha=None,
                last_run_at="2026-08-11T00:00:00Z",
                last_run_result="failure",
            ).to_entity()

    def test_thread_key_hash_matches_fixture(self) -> None:
        queue = self.fixture["queueMessage"]
        expected = self.fixture["tableEntities"]["conversation"]["RowKey"]
        self.assertEqual(
            conversation_row_key(queue["teamId"], queue["channelId"], queue["rootTs"]),
            expected,
        )

    def _cosmos_chunk(self, **overrides: object) -> CosmosChunk:
        expected = dict(self.fixture["cosmosChunk"])
        embedding_descriptor = expected.pop("embedding")
        embedding = tuple([embedding_descriptor["fill"]] * embedding_descriptor["dimensions"])
        values = {
            "id": expected["id"],
            "corpus_id": expected["corpusId"],
            "article_id": expected["articleId"],
            "chunk_index": expected["chunkIndex"],
            "slug": expected["slug"],
            "title": expected["title"],
            "emoji": expected["emoji"],
            "article_type": expected["articleType"],
            "topics": tuple(expected["topics"]),
            "published": expected["published"],
            "published_at": expected["publishedAt"],
            "heading": expected["heading"],
            "source_path": expected["sourcePath"],
            "source_url": expected["sourceUrl"],
            "source_revision": expected["sourceRevision"],
            "source_blob_sha": expected["sourceBlobSha"],
            "chunking_version": expected["chunkingVersion"],
            "indexed_at": expected["indexedAt"],
            "text": expected["text"],
            "embedding": embedding,
        }
        values.update(overrides)
        return CosmosChunk(**values)  # type: ignore[arg-type]

    def test_cosmos_chunk_matches_fixture(self) -> None:
        expected = dict(self.fixture["cosmosChunk"])
        embedding_descriptor = expected.pop("embedding")
        embedding = [embedding_descriptor["fill"]] * embedding_descriptor["dimensions"]
        chunk = self._cosmos_chunk()
        expected["embedding"] = list(embedding)
        self.assertEqual(chunk.to_document(), expected)

    def test_cosmos_chunk_rejects_wrong_embedding_size(self) -> None:
        with self.assertRaisesRegex(ContractError, str(EMBEDDING_DIMENSIONS)):
            self._cosmos_chunk(embedding=(0.0,))

    def test_cosmos_chunk_rejects_non_finite_embedding(self) -> None:
        embedding = [0.0] * EMBEDDING_DIMENSIONS
        embedding[-1] = float("nan")
        with self.assertRaisesRegex(ContractError, "finite"):
            self._cosmos_chunk(embedding=tuple(embedding))

    def test_cosmos_chunk_rejects_article_id_different_from_slug(self) -> None:
        with self.assertRaisesRegex(ContractError, "articleId must equal slug"):
            self._cosmos_chunk(article_id="different-slug", id="different-slug:0")

    def test_setting_and_resource_names_match_fixture(self) -> None:
        self.assertEqual(list(REQUIRED_SETTING_NAMES), self.fixture["settings"])
        self.assertEqual(
            {
                "stateTable": STATE_TABLE_NAME,
                "slackQueue": SLACK_QUEUE_NAME,
                "cosmosDatabase": COSMOS_DATABASE_NAME,
                "cosmosContainer": COSMOS_CONTAINER_NAME,
                "corpusId": CORPUS_ID,
            },
            self.fixture["resourceNames"],
        )


if __name__ == "__main__":
    unittest.main()
