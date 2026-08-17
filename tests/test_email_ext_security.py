"""Email extractor attachment depth and fan-out limits."""

from __future__ import annotations

from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from unittest.mock import AsyncMock, patch

import pytest

from nce.extractors.dispatch import _MAX_ATTACHMENTS_PER_MESSAGE
from nce.extractors.email_ext import extract_eml


@pytest.mark.asyncio
async def test_eml_attachment_fan_out_limit():
    msg = MIMEMultipart()
    msg.attach(MIMEApplication(b"x", Name="a.txt"))
    for i in range(_MAX_ATTACHMENTS_PER_MESSAGE + 5):
        msg.attach(MIMEApplication(b"y", Name=f"f{i}.txt"))

    calls = 0

    async def _fake_extract(*_a, **_kw):
        nonlocal calls
        calls += 1
        from nce.extractors.core import ExtractionResult

        return ExtractionResult(method="fake", text="ok", sections=[], metadata={})

    with patch(
        "nce.extractors.dispatch.extract_with_fallback",
        new=AsyncMock(side_effect=_fake_extract),
    ):
        result = await extract_eml(msg.as_bytes())

    assert calls <= _MAX_ATTACHMENTS_PER_MESSAGE
    assert any("attachment_limit" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_eml_nested_depth_limit():
    inner = MIMEMultipart()
    inner.attach(MIMEApplication(b"inner", Name="inner.txt"))
    outer = MIMEMultipart()
    outer.attach(MIMEApplication(inner.as_bytes(), Name="nested.eml"))

    depths: list[int] = []

    async def _fake_extract(*_a, attachment_depth=0, **_kw):
        depths.append(attachment_depth)
        from nce.extractors.core import ExtractionResult

        return ExtractionResult(method="fake", text="nested", sections=[], metadata={})

    with patch(
        "nce.extractors.dispatch.extract_with_fallback",
        new=AsyncMock(side_effect=_fake_extract),
    ):
        await extract_eml(outer.as_bytes())

    assert depths
    assert max(depths) >= 1
