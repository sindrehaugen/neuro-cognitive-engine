"""Shared LLM payload sanitization utilities.

Hardened against prompt injection, XML boundary escaping, and zero-width
character obfuscation.  Used by any module that embeds user-controlled text
into LLM prompts.
"""

from __future__ import annotations

import html as _html
import logging as _logging
import re
import unicodedata as _unicodedata

_MAX_PAYLOAD_LEN: int = 10_000

_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u200e\u200f\ufeff\u2060")

_INJECTION_PATTERNS = (
    re.compile(r"\{[^}]{0,200}\}", re.IGNORECASE),  # template injection
    re.compile(r"ignore\s+(?:previous|above|prior|all)", re.IGNORECASE),
)

_log = _logging.getLogger("nce.sanitize")


def sanitize_llm_payload(text: str) -> str:
    """Strip zero-width unicode spaces and neutralize XML/HTML tag markers.

    Defences applied (in order):

    1. **NFKC normalization** — collapses fullwidth and lookalike characters
       (e.g. ``＜`` U+FF1C → ``<``) so downstream checks use canonical forms.
    2. **Zero-width and control-char purge** — removes zero-width unicode,
       bidirectional markings, and non-printable control characters (preserves
       ``\\n``, ``\\r``, ``\\t``).
    3. **HTML entity decode** — ``&lt;script&gt;`` is treated the same as ``<script>``.
    4. **Tag stripping** — drops ``<tag>``, ``</tag>``, ``<tag attr="val">``
       via regex.
    4b. **Injection pattern logging** — warns on template-brace or
       ignore-previous-instruction patterns (content not logged).
    5. **Bracket neutralisation** — converts any remaining ``<`` / ``>`` to
       ``[`` / ``]`` so lone angle brackets cannot form new tags.
    6. **Curly brace escaping** — ``{`` / ``}`` doubled for downstream
       template safety.

    Output is truncated to ``_MAX_PAYLOAD_LEN`` characters.

    Returns ``""`` for empty input.
    """
    if not text:
        return ""

    # 1. NFKC normalization: collapse fullwidth/lookalike characters
    #    (e.g. ＜ U+FF1C → <) so all downstream checks work on canonical forms.
    text = _unicodedata.normalize("NFKC", text)

    # 2. Purge zero-width unicode, bidirectional markings, and non-printable controls
    text = "".join(
        c for c in text if c not in _ZERO_WIDTH and (c.isprintable() or c in ("\n", "\r", "\t"))
    )

    # 3. Decode HTML entities so &lt;script&gt; is treated the same as <script>
    text = _html.unescape(text)

    # 4. Drop XML/HTML-like tags
    text = re.sub(r"<\/?[a-zA-Z][^>]*>", "", text)

    # 4b. Detect and log suspicious injection patterns before neutralizing
    _suspicious = any(p.search(text) for p in _INJECTION_PATTERNS)
    if _suspicious:
        _log.warning(
            "sanitize_llm_payload: possible prompt injection pattern detected "
            "(input length=%d). Content neutralized.",
            len(text),
        )

    # 5. Neutralize any remaining angle brackets
    text = text.replace("<", "[").replace(">", "]")

    # 6. Escape curly braces for downstream template / f-string safety
    text = text.replace("{", "{{").replace("}", "}}")
    return text[:_MAX_PAYLOAD_LEN]
