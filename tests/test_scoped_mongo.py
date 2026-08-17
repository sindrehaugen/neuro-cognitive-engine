from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from nce.db_utils import scoped_mongo_session


# Minimal async iterator helper for find cursors
class AsyncIteratorMock:
    def __init__(self, items: list[dict]) -> None:
        self._items = list(items)
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


@pytest.mark.asyncio
async def test_scoped_mongo_session_basic_isolation():
    client = MagicMock()
    # Mock database
    db_mock = MagicMock()
    client.memory_archive = db_mock

    # Mock collection
    coll_mock = MagicMock()
    db_mock.episodes = coll_mock

    # Return cursor for find
    coll_mock.find.return_value = AsyncIteratorMock([])

    # Return AsyncMock for find_one/insert/etc
    coll_mock.find_one = AsyncMock(return_value=None)
    coll_mock.insert_one = AsyncMock(return_value=MagicMock(inserted_id="inserted_id"))
    coll_mock.insert_many = AsyncMock(return_value=MagicMock(inserted_ids=["id1"]))
    coll_mock.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    coll_mock.update_many = AsyncMock(return_value=MagicMock(modified_count=1))
    coll_mock.replace_one = AsyncMock(return_value=MagicMock(modified_count=1))
    coll_mock.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    coll_mock.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))

    ns_id = str(uuid4())

    async with scoped_mongo_session(client, ns_id) as s_db:
        # 1. Test find_one auto-scopes when namespace_id is missing
        await s_db.episodes.find_one({"foo": "bar"})
        coll_mock.find_one.assert_called_with({"foo": "bar", "namespace_id": ns_id})

        # 2. Test find auto-scopes
        s_db.episodes.find({"foo": "bar"})
        coll_mock.find.assert_called_with({"foo": "bar", "namespace_id": ns_id})

        # 3. Test matching namespace_id works
        await s_db.episodes.find_one({"foo": "bar", "namespace_id": ns_id})

        # 4. Test mismatched namespace_id raises ValueError
        with pytest.raises(ValueError, match="Mismatched namespace_id"):
            await s_db.episodes.find_one({"namespace_id": "mismatched-ns"})

        with pytest.raises(ValueError, match="Mismatched namespace_id"):
            s_db.episodes.find({"namespace_id": "mismatched-ns"})

        # 5. Test insert_one auto-scopes and verifies namespace_id
        await s_db.episodes.insert_one({"data": "payload"})
        coll_mock.insert_one.assert_called_with({"data": "payload", "namespace_id": ns_id})

        with pytest.raises(ValueError, match="Mismatched namespace_id"):
            await s_db.episodes.insert_one({"namespace_id": "mismatched-ns"})

        # 6. Test insert_many auto-scopes and verifies namespace_id
        await s_db.episodes.insert_many([{"data": "1"}, {"data": "2"}])
        coll_mock.insert_many.assert_called_with(
            [{"data": "1", "namespace_id": ns_id}, {"data": "2", "namespace_id": ns_id}]
        )

        with pytest.raises(ValueError, match="Mismatched namespace_id"):
            await s_db.episodes.insert_many([{"namespace_id": "mismatched-ns"}])

        # 7. Test update_one / update_many auto-scopes filters and prevents modifying namespace_id to mismatched value
        await s_db.episodes.update_one({"foo": "bar"}, {"$set": {"data": "new"}})
        coll_mock.update_one.assert_called_with(
            {"foo": "bar", "namespace_id": ns_id}, {"$set": {"data": "new"}}
        )

        with pytest.raises(ValueError, match="Cannot update namespace_id"):
            await s_db.episodes.update_one(
                {"foo": "bar"}, {"$set": {"namespace_id": "mismatched-ns"}}
            )

        # 8. Test replace_one auto-scopes filter and verifies replacement document
        await s_db.episodes.replace_one({"foo": "bar"}, {"data": "new"})
        coll_mock.replace_one.assert_called_with(
            {"foo": "bar", "namespace_id": ns_id}, {"data": "new", "namespace_id": ns_id}
        )

        with pytest.raises(ValueError, match="Mismatched namespace_id"):
            await s_db.episodes.replace_one({"foo": "bar"}, {"namespace_id": "mismatched-ns"})

        # 9. Test delete_one / delete_many auto-scopes
        await s_db.episodes.delete_one({"foo": "bar"})
        coll_mock.delete_one.assert_called_with({"foo": "bar", "namespace_id": ns_id})

        await s_db.episodes.delete_many({"foo": "bar"})
        coll_mock.delete_many.assert_called_with({"foo": "bar", "namespace_id": ns_id})
