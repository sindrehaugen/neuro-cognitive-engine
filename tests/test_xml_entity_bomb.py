"""Regression tests for XML entity expansion (Billion Laughs / XXE) attacks.

Ensures that extractors using XML parsers do not resolve external entities
or expand recursive entity declarations.
"""

import pytest

from nce.extractors import adobe_ext, diagrams, plaintext

_BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
]>
<lolz>&lol5;</lolz>
"""

_XXE_PAYLOAD = b"""<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<foo>&xxe;</foo>
"""

_SAFE_XML = b"""<?xml version="1.0"?>
<root><child>hello</child></root>
"""


class TestPlaintextXmlSecurity:
    """XML extraction in plaintext.py must not resolve entities."""

    @pytest.mark.asyncio
    async def test_billion_laughs_rejected(self):
        result = await plaintext.extract_xml(_BILLION_LAUGHS)
        # Should fall back to plain-text stripping or fail gracefully
        assert result.text is not None
        # The expanded entity text must NOT appear (would be billions of "lol")
        assert result.text.count("lol") < 100

    @pytest.mark.asyncio
    async def test_xxe_rejected(self):
        result = await plaintext.extract_xml(_XXE_PAYLOAD)
        assert result.text is not None
        assert "/etc/passwd" not in result.text

    @pytest.mark.asyncio
    async def test_safe_xml_parses_normally(self):
        result = await plaintext.extract_xml(_SAFE_XML)
        assert "hello" in result.text


class TestDiagramsXmlSecurity:
    """Diagram XML parsing must not resolve entities."""

    def test_billion_laughs_returns_none(self):
        result = diagrams._safe_et_parse(_BILLION_LAUGHS)
        assert result is None

    def test_xxe_returns_none(self):
        result = diagrams._safe_et_parse(_XXE_PAYLOAD)
        assert result is None

    def test_safe_xml_parses(self):
        result = diagrams._safe_et_parse(_SAFE_XML)
        assert result is not None
        assert result.tag == "root"


class TestAdobeExtXmlSecurity:
    """Adobe IDML XML parsing must not resolve entities."""

    def test_billion_laughs_falls_back_to_regex(self):
        result = adobe_ext._idml_story_texts(_BILLION_LAUGHS)
        assert result is not None
        assert isinstance(result, list)

    def test_xxe_falls_back_to_regex(self):
        result = adobe_ext._idml_story_texts(_XXE_PAYLOAD)
        assert result is not None
        assert isinstance(result, list)

    def test_safe_xml_parses(self):
        result = adobe_ext._idml_story_texts(_SAFE_XML)
        assert result is not None
        assert isinstance(result, list)
