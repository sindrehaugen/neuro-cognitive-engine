"""Unit tests for nce.mongo_bulk (FIX-021 / FIX-024 hardened batch reads)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from bson import ObjectId

from nce.mongo_bulk import (
    _fetch_field_by_refs,
    normalize_payload_ref,
)

_LOGGER = "nce.mongo_bulk"


class AsyncIteratorMock:
    """Minimal async cursor for ``collection.find`` return values."""

    def __init__(self, items: list[dict]) -> None:
        self._items = list(items)
        self._index = 0

    def __aiter__(self) -> AsyncIteratorMock:
        return self

    async def __anext__(self) -> dict:
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


def _collection() -> MagicMock:
    coll = MagicMock()
    coll.name = "episodes"
    coll.find.return_value = AsyncIteratorMock([])
    return coll


# ---------------------------------------------------------------------------
# normalize_payload_ref
# ---------------------------------------------------------------------------


class TestNormalizePayloadRef:
    def test_none_is_falsy(self) -> None:
        assert normalize_payload_ref(None) is None

    def test_object_id_becomes_hex_string(self) -> None:
        oid = ObjectId()
        assert normalize_payload_ref(oid) == str(oid)

    def test_hex_string_unchanged(self) -> None:
        s = f"{42:024x}"
        assert normalize_payload_ref(s) == s

    def test_empty_string_is_falsy(self) -> None:
        assert normalize_payload_ref("") == ""
        assert not normalize_payload_ref("")


# ---------------------------------------------------------------------------
# Field allowlist
# ---------------------------------------------------------------------------


class TestFieldAllowlist:
    @pytest.mark.asyncio
    async def test_raw_data_accepted(self) -> None:
        coll = _collection()
        oid = ObjectId()
        key = str(oid)
        coll.find.return_value = AsyncIteratorMock([{"_id": oid, "raw_data": "payload"}])
        out = await _fetch_field_by_refs(coll, [key], field="raw_data")
        assert out == {key: "payload"}
        coll.find.assert_called_once()

    @pytest.mark.asyncio
    async def test_raw_code_accepted(self) -> None:
        coll = _collection()
        oid = ObjectId()
        key = str(oid)
        coll.find.return_value = AsyncIteratorMock([{"_id": oid, "raw_code": "source"}])
        out = await _fetch_field_by_refs(coll, [key], field="raw_code")
        assert out == {key: "source"}

    @pytest.mark.asyncio
    async def test_disallowed_field_raises_value_error(self) -> None:
        coll = _collection()
        with pytest.raises(ValueError, match="not allowed"):
            await _fetch_field_by_refs(coll, [], field="__secret__")
        coll.find.assert_not_called()


# ---------------------------------------------------------------------------
# Max refs guard
# ---------------------------------------------------------------------------


class TestMaxRefsGuard:
    @pytest.mark.asyncio
    async def test_over_limit_raises_with_limit_in_message(self) -> None:
        coll = _collection()
        refs = [f"{i:024x}" for i in range(10_001)]
        with pytest.raises(ValueError) as exc_info:
            await _fetch_field_by_refs(coll, refs, field="raw_data")
        assert "10000" in str(exc_info.value)
        coll.find.assert_not_called()

    @pytest.mark.asyncio
    async def test_exactly_max_refs_completes(self) -> None:
        coll = _collection()
        refs = [f"{i:024x}" for i in range(10_000)]
        out = await _fetch_field_by_refs(coll, refs, field="raw_data")
        assert out == {}
        assert coll.find.call_count == 20


# ---------------------------------------------------------------------------
# ObjectId handling
# ---------------------------------------------------------------------------


class TestObjectIdHandling:
    @pytest.mark.asyncio
    async def test_invalid_strings_skipped_valid_fetched(self) -> None:
        coll = _collection()
        valid_key = f"{1:024x}"
        coll.find.return_value = AsyncIteratorMock([{"_id": ObjectId(valid_key), "raw_data": "ok"}])
        out = await _fetch_field_by_refs(
            coll, ["not-a-valid-object-id", valid_key], field="raw_data"
        )
        assert out == {valid_key: "ok"}
        coll.find.assert_called_once()
        batch = coll.find.call_args[0][0]["_id"]["$in"]
        assert batch == [ObjectId(valid_key)]

    @pytest.mark.asyncio
    async def test_mix_valid_invalid_only_valid_in_query(self) -> None:
        coll = _collection()
        k1, k2 = f"{10:024x}", f"{11:024x}"
        coll.find.return_value = AsyncIteratorMock(
            [
                {"_id": ObjectId(k1), "raw_data": "a"},
                {"_id": ObjectId(k2), "raw_data": "b"},
            ]
        )
        invalid_24 = "g" * 24
        await _fetch_field_by_refs(coll, [k1, invalid_24, k2], field="raw_data")
        batch = coll.find.call_args[0][0]["_id"]["$in"]
        assert set(batch) == {ObjectId(k1), ObjectId(k2)}
        assert len(batch) == 2

    @pytest.mark.asyncio
    async def test_all_invalid_returns_empty_without_find(self) -> None:
        coll = _collection()
        out = await _fetch_field_by_refs(coll, ["g" * 24, "not-hex-at-all!!!!"], field="raw_data")
        assert out == {}
        coll.find.assert_not_called()


# ---------------------------------------------------------------------------
# Batching & query options
# ---------------------------------------------------------------------------


class TestBatchingAndTimeout:
    @pytest.mark.asyncio
    async def test_six_hundred_refs_batches_find_twice(self) -> None:
        coll = _collection()
        refs = [f"{i:024x}" for i in range(600)]
        await _fetch_field_by_refs(coll, refs, field="raw_data")
        assert coll.find.call_count == 2
        first_batch = coll.find.call_args_list[0][0][0]["_id"]["$in"]
        second_batch = coll.find.call_args_list[1][0][0]["_id"]["$in"]
        assert len(first_batch) == 500
        assert len(second_batch) == 100

    @pytest.mark.asyncio
    async def test_every_find_uses_max_time_ms(self) -> None:
        coll = _collection()
        refs = [f"{i:024x}" for i in range(600)]
        await _fetch_field_by_refs(coll, refs, field="raw_data")
        for call in coll.find.call_args_list:
            assert call.kwargs.get("max_time_ms") == 5000


# ---------------------------------------------------------------------------
# Partial failure & logging
# ---------------------------------------------------------------------------


class TestPartialFailureAndLogging:
    @pytest.mark.asyncio
    async def test_second_batch_failure_returns_first_batch(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coll = _collection()
        refs = [f"{i:024x}" for i in range(501)]
        first_batch_oids = [ObjectId(r) for r in refs[:500]]

        call_num = 0

        def find_side_effect(filter_doc, projection=None, max_time_ms=None):
            nonlocal call_num
            call_num += 1
            batch = filter_doc["_id"]["$in"]
            if call_num == 1:
                docs = [{"_id": oid, "raw_data": f"v-{i}"} for i, oid in enumerate(batch)]
                return AsyncIteratorMock(docs)
            raise ConnectionError("second batch down")

        coll.find = MagicMock(side_effect=find_side_effect)

        caplog.set_level(logging.DEBUG, logger=_LOGGER)
        out = await _fetch_field_by_refs(coll, refs, field="raw_data")

        assert len(out) == 500
        assert out[str(first_batch_oids[0])] == "v-0"
        assert coll.find.call_count == 2

        batch_fail_warnings = [
            r
            for r in caplog.records
            if "Batch Mongo hydrate failed" in r.getMessage() and r.levelname == "WARNING"
        ]
        batch_fail_errors = [
            r
            for r in caplog.records
            if "Batch Mongo hydrate failed" in r.getMessage() and r.levelname == "ERROR"
        ]
        assert not batch_fail_warnings
        assert len(batch_fail_errors) == 1

    @pytest.mark.asyncio
    async def test_invalid_24_char_key_logs_prefix_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coll = _collection()
        bad_key = "g" * 24
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        out = await _fetch_field_by_refs(coll, [bad_key], field="raw_data")
        assert out == {}
        coll.find.assert_not_called()

        warn_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warn_records) >= 1
        for rec in warn_records:
            msg = rec.getMessage()
            assert bad_key not in msg
            assert "gggggggg" in msg
