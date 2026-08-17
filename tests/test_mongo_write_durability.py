from unittest.mock import AsyncMock, MagicMock, PropertyMock
from uuid import uuid4

import pytest

from nce.models import AssertionType, MemoryType, StoreMemoryRequest


def _make_payload():
    return StoreMemoryRequest(
        namespace_id=uuid4(),
        agent_id="test-agent",
        content="Hello world",
        summary="Test summary",
        heavy_payload="Heavy payload",
        memory_type=MemoryType.episodic,
        assertion_type=AssertionType.fact,
        metadata={"user_id": "user-1", "session_id": "sess-1"},
        check_contradictions=False,
    )


class DummyCollection:
    def __init__(self):
        self.with_options_called = False
        self.with_options_kwargs = {}
        self.insert_called = False
        self._collection = self  # So db.episodes._collection returns self

    def with_options(self, **kwargs):
        self.with_options_called = True
        self.with_options_kwargs = kwargs
        return self

    async def insert_one(self, document, *args, **kwargs):
        self.insert_called = True

        class FakeResult:
            inserted_id = "507f1f77bcf86cd799439011"

        return FakeResult()


@pytest.mark.asyncio
async def test_mongo_write_concern_durability():
    from nce.orchestrators.memory import MemoryOrchestrator

    # Mock postgres pool
    pg_pool = MagicMock()

    # Mock redis client
    redis_client = AsyncMock()

    # Mock mongo client
    mongo_client = MagicMock()
    db_mock = MagicMock()
    mongo_client.memory_archive = db_mock

    dummy_collection = DummyCollection()
    type(db_mock).episodes = PropertyMock(return_value=dummy_collection)

    orchestrator = MemoryOrchestrator(
        pg_pool=pg_pool, mongo_client=mongo_client, redis_client=redis_client
    )

    # Call _store_episodic_mongodb
    payload = _make_payload()

    # Mock PII processing output
    pii_result = MagicMock()
    pii_result.redacted = False
    pii_result.entities_found = 0

    (
        inserted_mongo_id,
        inserted_result,
        wrapped_dek,
        dek_key_id,
    ) = await orchestrator._store_episodic_mongodb(payload, "sanitized_heavy", pii_result)

    # Verify our write concern assertions!
    assert dummy_collection.with_options_called is True
    wc = dummy_collection.with_options_kwargs.get("write_concern")
    assert wc is not None
    assert wc.document == {"w": "majority", "j": True}
    assert dummy_collection.insert_called is True
