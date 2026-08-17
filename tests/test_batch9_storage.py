from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from minio import Minio

from nce.extractors.core import ExtractionResult, Section
from nce.storage import generate_secure_presigned_url

# =============================================================================
# Pre-signed URL Security Tests
# =============================================================================


def test_presigned_url_tenant_isolation():
    minio_mock = MagicMock(spec=Minio)
    ns_id = uuid4()

    # Valid tenant path should succeed
    valid_object = f"{ns_id}/session-1/artifact.png"
    minio_mock.presigned_get_object.return_value = "http://valid-url"

    url = generate_secure_presigned_url(
        minio_client=minio_mock,
        bucket_name="mcp-image",
        object_name=valid_object,
        method="GET",
        current_namespace_id=ns_id,
    )
    assert url == "http://valid-url"

    # Invalid tenant path should raise PermissionError
    invalid_object = f"{uuid4()}/session-1/artifact.png"
    with pytest.raises(PermissionError) as exc_info:
        generate_secure_presigned_url(
            minio_client=minio_mock,
            bucket_name="mcp-image",
            object_name=invalid_object,
            method="GET",
            current_namespace_id=ns_id,
        )
    assert "Access denied: Tenant path mismatch" in str(exc_info.value)


def test_presigned_url_expiry_bounding():
    minio_mock = MagicMock(spec=Minio)
    ns_id = uuid4()
    object_name = f"{ns_id}/session-1/file.txt"

    # Expiry > 900 seconds should be clamped to 900 seconds
    generate_secure_presigned_url(
        minio_client=minio_mock,
        bucket_name="mcp-document",
        object_name=object_name,
        current_namespace_id=ns_id,
        method="GET",
        expiry_seconds=3600,  # 1 hour
    )

    # Assert that minio client was called with 15 mins (900s) timedelta
    minio_mock.presigned_get_object.assert_called_once()
    kwargs = minio_mock.presigned_get_object.call_args[1]
    assert kwargs["expires"] == timedelta(seconds=900)


def test_presigned_url_put_validation():
    minio_mock = MagicMock(spec=Minio)
    ns_id = uuid4()

    # Valid extension should succeed
    generate_secure_presigned_url(
        minio_client=minio_mock,
        bucket_name="mcp-document",
        object_name=f"{ns_id}/session-1/document.pdf",
        current_namespace_id=ns_id,
        method="PUT",
    )

    # Invalid extension should fail
    with pytest.raises(ValueError) as exc_info:
        generate_secure_presigned_url(
            minio_client=minio_mock,
            bucket_name="mcp-document",
            object_name=f"{ns_id}/session-1/malicious.exe",
            current_namespace_id=ns_id,
            method="PUT",
        )
    assert "Unsupported file extension" in str(exc_info.value)


# =============================================================================
# Saga Rollback Tests
# =============================================================================


@pytest.mark.asyncio
async def test_store_artifact_saga_rollback(monkeypatch):
    from nce.models import ArtifactPayload
    from nce.orchestrators.memory import MemoryOrchestrator

    # Setup orchestrator with mocks
    pg_pool = MagicMock()
    mongo_client = MagicMock()
    redis_client = MagicMock()
    minio_client = MagicMock(spec=Minio)

    orchestrator = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=mongo_client,
        redis_client=redis_client,
        minio_client=minio_client,
    )

    # Mock file system path resolver
    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)

    # Mock store_memory to fail, triggering rollback
    orchestrator.store_memory = AsyncMock(side_effect=RuntimeError("Saga DB Failure"))

    payload = ArtifactPayload(
        namespace_id=uuid4(),
        user_id="user1",
        session_id="session1",
        media_type="image",
        file_path_on_disk="test.png",
        summary="Test artifact",
    )

    with pytest.raises(RuntimeError) as exc_info:
        await orchestrator.store_artifact(payload)

    assert "Saga DB Failure" in str(exc_info.value)

    # Verify MinIO upload happened, and then removal was called during rollback
    minio_client.fput_object.assert_called_once()
    minio_client.remove_object.assert_called_once()


# =============================================================================
# Garbage Collector Pruning Tests
# =============================================================================


@pytest.mark.asyncio
async def test_garbage_collector_minio_pruning():
    from nce.garbage_collector import _collect_minio_orphans

    minio_mock = MagicMock(spec=Minio)

    # Mock buckets
    b1 = MagicMock()
    b1.name = "mcp-image"
    b2 = MagicMock()
    b2.name = "mcp-document"
    b3 = MagicMock()
    b3.name = "not-mcp-bucket"

    minio_mock.list_buckets.return_value = [b1, b2, b3]

    # Mock objects in mcp-image (referenced vs orphaned)
    ref_obj = MagicMock()
    ref_obj.is_dir = False
    ref_obj.object_name = "ns-1/session-1/img1.png"
    ref_obj.last_modified = datetime.now(timezone.utc) - timedelta(days=2)  # Old

    orphan_old_obj = MagicMock()
    orphan_old_obj.is_dir = False
    orphan_old_obj.object_name = "ns-1/session-1/img2.png"
    orphan_old_obj.last_modified = datetime.now(timezone.utc) - timedelta(
        days=2
    )  # Old, unreferenced

    orphan_young_obj = MagicMock()
    orphan_young_obj.is_dir = False
    orphan_young_obj.object_name = "ns-1/session-1/img3.png"
    orphan_young_obj.last_modified = datetime.now(timezone.utc) - timedelta(
        minutes=5
    )  # Young, unreferenced

    minio_mock.list_objects.side_effect = lambda bucket_name, recursive: {
        "mcp-image": [ref_obj, orphan_old_obj, orphan_young_obj],
        "mcp-document": [],
    }[bucket_name]

    # Reference set (ref_obj is referenced, the others are not)
    minio_refs = {"ns-1/session-1/img1.png"}

    deleted_count = await _collect_minio_orphans(minio_mock, minio_refs)

    # Should only delete orphan_old_obj (1 deletion)
    assert deleted_count == 1
    minio_mock.remove_object.assert_called_once_with("mcp-image", "ns-1/session-1/img2.png")


# =============================================================================
# Extractor Memory Limit and GC Tests
# =============================================================================


@pytest.mark.asyncio
async def test_pdf_extractor_page_limit_and_gc():
    from nce.extractors.pdf_ext import extract_pdf

    long_text = "Page text " * 30
    # Mock _check_pdf_bomb, is_pdf_encrypted_blob, _pymupdf_extract_sync, and _pypdf_extract_sync
    with (
        patch("nce.extractors.pdf_ext._check_pdf_bomb", return_value=None),
        patch("nce.extractors.pdf_ext.is_pdf_encrypted_blob", return_value=False),
        patch(
            "nce.extractors.pdf_ext._pymupdf_extract_sync",
            return_value=(
                long_text,
                [Section(text=long_text, structure_path="Page 1", section_type="body", order=0)],
                [],
            ),
        ),
        patch(
            "nce.extractors.pdf_ext._pypdf_extract_sync",
            return_value=(
                long_text,
                [Section(text=long_text, structure_path="Page 1", section_type="body", order=0)],
                [],
            ),
        ),
        patch("nce.extractors.pdf_ext._merge_pdfplumber_tables", return_value=None),
    ):
        # Capture garbage collection calls
        with patch("gc.collect") as mock_gc:
            res = await extract_pdf(b"%PDF-1.4 test blob")
            assert res.text == long_text
            # Verify gc.collect() was explicitly triggered
            mock_gc.assert_called()


@pytest.mark.asyncio
async def test_dispatch_gc_collection():
    from nce.extractors.dispatch import extract_bytes

    mock_handler = AsyncMock(
        return_value=ExtractionResult(
            method="txt", text="hello", sections=[], metadata={}, warnings=[]
        )
    )

    # Mock plaintext extractor using patch.dict
    with (
        patch("nce.extractors.dispatch._initialized", True),
        patch("nce.extractors.dispatch._resolve_ext", return_value="txt"),
        patch("nce.extractors.dispatch.maybe_encrypted_skip", return_value=None),
        patch.dict("nce.extractors.dispatch._REGISTRY", {"txt": mock_handler}),
    ):
        with patch("gc.collect") as mock_gc:
            res = await extract_bytes(b"hello text content", "test.txt")
            assert res.text == "hello"
            # Verify gc.collect() was explicitly called in dispatch.py success path
            mock_gc.assert_called()


@pytest.mark.asyncio
async def test_store_artifact_object_name_prefix(monkeypatch):
    from nce.models import ArtifactPayload
    from nce.orchestrators.memory import MemoryOrchestrator

    pg_pool = MagicMock()
    mongo_client = MagicMock()
    redis_client = MagicMock()
    minio_client = MagicMock(spec=Minio)

    orchestrator = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=mongo_client,
        redis_client=redis_client,
        minio_client=minio_client,
    )

    monkeypatch.setattr("pathlib.Path.is_file", lambda self: True)
    orchestrator.store_memory = AsyncMock(return_value={"payload_ref": "some_ref"})

    ns_id = uuid4()
    payload = ArtifactPayload(
        namespace_id=ns_id,
        user_id="user1",
        session_id="session1",
        media_type="image",
        file_path_on_disk="test.png",
        summary="Test artifact",
    )

    res = await orchestrator.store_artifact(payload)
    assert res == "some_ref"

    # Assert that fput_object was called with an object_name starting with f"{ns_id}/"
    minio_client.fput_object.assert_called_once()
    args, kwargs = minio_client.fput_object.call_args
    # signature: fput_object(bucket_name, object_name, file_path)
    called_object_name = args[1]
    assert called_object_name.startswith(f"{ns_id}/")


@pytest.mark.asyncio
async def test_shred_memory_cross_tenant_denied():
    from nce.orchestrators.memory import MemoryOrchestrator

    pg_pool = MagicMock()
    mongo_client = MagicMock()
    redis_client = MagicMock()
    minio_client = MagicMock(spec=Minio)

    orchestrator = MemoryOrchestrator(
        pg_pool=pg_pool,
        mongo_client=mongo_client,
        redis_client=redis_client,
        minio_client=minio_client,
    )

    ns_id = uuid4()
    other_ns_id = uuid4()
    memory_id = uuid4()

    mock_conn = AsyncMock()
    mock_conn.fetchrow.return_value = {
        "payload_ref": "507f1f77bcf86cd799439011",
        "dek_key_id": "key-1",
        "was_encrypted": True,
        "user_id": "user1",
        "session_id": "session1",
        "agent_id": "agent1",
        "metadata": {"bucket": "mcp-image", "object_name": f"{other_ns_id}/session1/file.png"},
    }

    mock_conn.execute.return_value = "UPDATE 1"
    mock_conn.fetch.return_value = []

    mock_tx = AsyncMock()
    mock_conn.transaction = MagicMock(return_value=mock_tx)

    class MockSession:
        async def __aenter__(self):
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with (
        patch("nce.orchestrators.memory.scoped_pg_session", return_value=MockSession()),
        patch("nce.event_log.append_event", AsyncMock()),
    ):
        res = await orchestrator.shred_memory(
            memory_id=str(memory_id), namespace_id=str(ns_id), agent_id="system"
        )

    warnings = res["receipt"]["warnings"]
    assert len(warnings) > 0
    assert any(
        "Access denied: Object name does not belong to this namespace" in w for w in warnings
    )

    # Verify that remove_object was NEVER called
    minio_client.remove_object.assert_not_called()
