from uuid import uuid4

import pytest

from nce import ArtifactPayload as RootArtifactPayload
from nce import NCEEngine, StoreArtifactRequest
from nce.models import ArtifactPayload, MediaPayload
from nce.models import StoreArtifactRequest as ModelsStoreArtifactRequest


@pytest.mark.asyncio
async def test_store_artifact_delegation():
    """Verify store_artifact delegates to the same logic as store_media and handles aliases."""
    engine = NCEEngine()

    # Check if ArtifactPayload and StoreArtifactRequest are aliases for MediaPayload
    assert ArtifactPayload is MediaPayload
    assert StoreArtifactRequest is MediaPayload
    assert ModelsStoreArtifactRequest is MediaPayload
    assert RootArtifactPayload is MediaPayload

    # Verify both artifact_type and legacy media_type work and normalize "other" -> "binary"
    ns = uuid4()
    req_pdf = StoreArtifactRequest(
        namespace_id=ns,
        artifact_type="pdf",
        file_path_on_disk="spec.pdf",
        summary="Spec document",
    )
    assert req_pdf.artifact_type == "pdf"
    assert req_pdf.media_type == "pdf"
    assert req_pdf.user_id == "system"
    assert req_pdf.session_id == "default"

    req_legacy = ArtifactPayload(
        namespace_id=ns,
        media_type="other",
        file_path_on_disk="blob.bin",
        summary="Legacy other payload",
    )
    assert req_legacy.artifact_type == "binary"
    assert req_legacy.media_type == "binary"

    # Verify methods exist
    assert hasattr(engine, "store_artifact")
    assert hasattr(engine, "store_media")


@pytest.mark.asyncio
async def test_orchestrator_artifact_alias():
    """Verify the orchestrator has the correct artifact methods."""
    engine = NCEEngine()

    assert "store_artifact" in dir(engine)
    assert engine.store_media.__doc__ and "[DEPRECATED]" in engine.store_media.__doc__
