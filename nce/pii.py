"""
Phase 0.3 — PII Detection and Auto-Redaction Pipeline.
Intercepts payloads before they hit the LLM provider interface and masks sensitive entities.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
from typing import TYPE_CHECKING

from nce.models import NamespacePIIConfig, PIIEntity, PIIPolicy, PIIProcessResult
from nce.signing import (
    MasterKeyMissingError,
    SecureKeyBuffer,
    encrypt_signing_key,
    require_master_key,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger("nce-pii")

# Pseudonym tokens: first 16 bytes of HMAC-SHA256 (128 bits), base64url-encoded
# (~22 chars).  Per-namespace key must provide at least 64 bits of key material
# (UTF-8 length ≥ 8).  128-bit collision resistance (2^64 birthday bound) is
# adequate for pseudonyms within a single namespace.
_MIN_PSEUDONYM_SECRET_BYTES = 8
_MAX_TEXT_BYTES: int = 1_000_000
_MAX_ENTITIES: int = 1_000


def _luhn_valid(digits: str) -> bool:
    """Luhn algorithm check — rejects non-card numeric sequences."""
    stripped = re.sub(r"[ -]", "", digits)
    total = 0
    reverse_digits = stripped[::-1]
    for i, ch in enumerate(reverse_digits):
        if not ch.isdigit():
            return False
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


# Simple regex fallback for environments without Presidio installed
_FALLBACK_REGEXES = {
    "EMAIL": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    "PHONE": r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "CREDIT_CARD": r"\b(?:\d[ -]*?){13,16}\b",
}

# ---------------------------------------------------------------------------
# Norwegian / EU PII patterns (locale="no")
# ---------------------------------------------------------------------------

# Fødselsnummer: DDMMYYNNNCC (11 contiguous digits, no separators in canonical form).
# The regex matches the digit structure; _valid_fodselsnummer() verifies both
# Mod-11 check digits so we don't redact random 11-digit sequences.
_RE_FODSELSNUMMER = re.compile(r"\b(\d{11})\b")

# Norwegian organisation number: 9 digits, first digit is 8 or 9 (Brønnøysund).
# B-numbers (temporary IDs) and D-numbers start with 8; ordinary orgs start with 9.
_RE_ORG_NUMBER = re.compile(r"\b([89]\d{8})\b")

# Norwegian mobile: 8 digits starting with 4 or 9, optionally prefixed by +47 / 0047.
# Use negative lookbehind/lookahead for digit boundaries instead of \b to handle
# compact forms like +4798765432 (no space between prefix and number).
_RE_NO_MOBILE = re.compile(r"(?<!\d)(?:\+47|0047)?[ -]?([49]\d{7})(?!\d)")

# EU generic national ID: catch-all for non-NO locales (passthrough hook).
# Currently a no-op placeholder; extend per country as needed.
_RE_EU_NATIONAL_ID: re.Pattern | None = None  # Reserved for future EU-ID patterns


def _fodselsnummer_check(digits: str) -> bool:
    """Validate both Mod-11 check digits of a Norwegian Fødselsnummer.

    Weights for first check digit (index 9):  [3, 7, 6, 1, 8, 9, 4, 5, 2]
    Weights for second check digit (index 10): [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    A remainder of 0 is valid; 1 is always invalid (no valid number produces it).
    """
    if len(digits) != 11 or not digits.isdigit():
        return False
    d = [int(c) for c in digits]
    # First check digit
    w1 = [3, 7, 6, 1, 8, 9, 4, 5, 2]
    s1 = sum(w * v for w, v in zip(w1, d[:9])) % 11
    c1 = 0 if s1 == 0 else 11 - s1
    if c1 == 10 or c1 != d[9]:
        return False
    # Second check digit
    w2 = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    s2 = sum(w * v for w, v in zip(w2, d[:10])) % 11
    c2 = 0 if s2 == 0 else 11 - s2
    return c2 != 10 and c2 == d[10]


def _scan_no_pii(text: str) -> list[PIIEntity]:
    """Scan for Norwegian-locale PII (Fødselsnummer, org numbers, mobile numbers).

    Returns entities sorted by start position ascending (before merge/dedup).
    This function is dependency-free — no spaCy or Presidio import required.
    """
    entities: list[PIIEntity] = []

    for m in _RE_FODSELSNUMMER.finditer(text):
        if _fodselsnummer_check(m.group(1)):
            entities.append(
                PIIEntity(
                    start=m.start(),
                    end=m.end(),
                    entity_type="NO_FODSELSNUMMER",
                    value=m.group(0),
                    score=0.99,
                )
            )

    for m in _RE_ORG_NUMBER.finditer(text):
        entities.append(
            PIIEntity(
                start=m.start(),
                end=m.end(),
                entity_type="NO_ORG_NUMBER",
                value=m.group(0),
                score=0.85,
            )
        )

    for m in _RE_NO_MOBILE.finditer(text):
        entities.append(
            PIIEntity(
                start=m.start(),
                end=m.end(),
                entity_type="NO_PHONE_MOBILE",
                value=m.group(0),
                score=0.90,
            )
        )

    return entities


def _merge_overlapping_entities(entities: list[PIIEntity]) -> list[PIIEntity]:
    """Remove or trim overlapping entity spans, keeping the highest-score span.

    Input must be sorted by start ascending. Returns list sorted by start
    descending (ready for reverse-order string replacement).
    """
    if not entities:
        return []
    entities = sorted(entities, key=lambda e: (e.start, -(e.end - e.start)))
    merged: list[PIIEntity] = []
    last_end = -1
    for ent in entities:
        if ent.start >= last_end:
            merged.append(ent)
            last_end = ent.end
    merged.sort(key=lambda e: e.start, reverse=True)
    return merged


_ANALYZER: object | None = None


def _get_analyzer() -> object:
    global _ANALYZER
    if _ANALYZER is None:
        from presidio_analyzer import AnalyzerEngine

        _ANALYZER = AnalyzerEngine()
    return _ANALYZER


def _scan_sync(text: str, config: NamespacePIIConfig, locale: str = "en") -> list[PIIEntity]:
    """
    Synchronous PII scan — executed in a thread pool so the regex/Presidio
    work does not block the async event loop.
    """
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(
            f"text exceeds maximum size for PII scanning "
            f"({len(text.encode('utf-8'))} bytes, limit {_MAX_TEXT_BYTES})"
        )

    if not config.entity_types:
        return []

    entities: list[PIIEntity] = []
    _allowlist_lower = {v.lower() for v in config.allowlist}

    try:
        try:
            from presidio_analyzer import AnalyzerEngine  # noqa: F401

            analyzer = _get_analyzer()
            results = analyzer.analyze(text=text, entities=config.entity_types, language="en")
            for r in results:
                value = text[r.start : r.end]
                if value.lower() not in _allowlist_lower:
                    entities.append(
                        PIIEntity(
                            start=r.start,
                            end=r.end,
                            entity_type=r.entity_type,
                            value=value,
                            score=r.score,
                        )
                    )
                del value  # Scrub local PII reference immediately after use
        except ImportError:
            # Fallback to regex — wrap each match in a try/finally so the raw
            # match value is never present in a live stack frame if an exception
            # propagates.  Sentry and Python logging capture local variables
            # from traceback frames; keeping ``value`` alive would leak PII into
            # error reports, violating GDPR Art. 25 (data protection by design).
            for entity_type in config.entity_types:
                pattern = _FALLBACK_REGEXES.get(entity_type)
                if not pattern:
                    continue
                for match in re.finditer(pattern, text):
                    value = None  # type: ignore[no-redef]
                    try:
                        value = match.group(0)
                        if entity_type == "CREDIT_CARD" and not _luhn_valid(value):
                            continue
                        if value.lower() not in _allowlist_lower:
                            entities.append(
                                PIIEntity(
                                    start=match.start(),
                                    end=match.end(),
                                    entity_type=entity_type,
                                    value=value,
                                    score=0.8,
                                )
                            )
                    except Exception as match_exc:
                        # Scrub the raw match value from the local frame before
                        # the exception is logged.  Log position/type only —
                        # never the matched text.
                        value = None  # overwrite before del to ensure GC sees null
                        del value
                        log.warning(
                            "PII regex match processing failed for entity_type=%r "
                            "at span=(%d, %d); skipping match. err=%s",
                            entity_type,
                            match.start(),
                            match.end(),
                            type(match_exc).__name__,  # type name only, not message
                        )
                        continue
                    finally:
                        # Always delete the local reference so it does not
                        # appear in frame locals if an outer exception unwinds
                        # through this scope.
                        value = None
                        del value

        # Norwegian locale: always append Norwegian-specific patterns in addition
        # to the generic EU set. Clear raw values from any NO entities on error.
        if locale == "no":
            no_entities = _scan_no_pii(text)
            entities.extend(no_entities)

        if len(entities) > _MAX_ENTITIES:
            for entity in entities:
                entity.clear_raw_value()
            raise ValueError(
                f"PII scan produced {len(entities)} entities, exceeding the limit of {_MAX_ENTITIES}. "
                "Split the text into smaller chunks."
            )

        return _merge_overlapping_entities(entities)
    except Exception:
        # If scan() fails partway through, clear raw PII values from any
        # entities already created so they never leak into tracebacks.
        for entity in entities:
            entity.clear_raw_value()
        raise


async def scan(text: str, config: NamespacePIIConfig, *, locale: str = "en") -> list[PIIEntity]:
    """Scans the text for PII entities defined in the namespace config.

    Uses Presidio if available, otherwise falls back to basic regex.
    When ``locale="no"``, Norwegian-specific patterns (Fødselsnummer, org numbers,
    mobile numbers) are applied in addition to the generic EU set.
    Offloaded to a thread pool so CPU-bound regex work never blocks the loop.
    """
    return await asyncio.to_thread(_scan_sync, text, config, locale)


def _pseudonym_hmac_key_material(
    config: NamespacePIIConfig, *, namespace_id: str
) -> SecureKeyBuffer:
    """Return HMAC key bytes for pseudonym generation."""
    if config.pseudonym_hmac_key is not None:
        key = config.pseudonym_hmac_key.encode("utf-8")
        if len(key) < _MIN_PSEUDONYM_SECRET_BYTES:
            raise ValueError(
                f"pseudonym_hmac_key must be at least {_MIN_PSEUDONYM_SECRET_BYTES} bytes "
                f"when set; got {len(key)}."
            )
        return SecureKeyBuffer(key)
    try:
        with require_master_key() as mk:
            key_view = mk.key_bytes
            if len(key_view) < 32:
                raise ValueError(
                    "Pseudonymisation requires NCE_MASTER_KEY (≥32 UTF-8 bytes) or "
                    f"a namespace pseudonym_hmac_key (≥{_MIN_PSEUDONYM_SECRET_BYTES} bytes)."
                )
            derived = hmac.new(
                bytes(key_view), namespace_id.encode("utf-8"), hashlib.sha256
            ).digest()
            return SecureKeyBuffer(derived)
    except MasterKeyMissingError:
        raise ValueError(
            "Pseudonymisation requires NCE_MASTER_KEY (≥32 UTF-8 bytes) or "
            f"a namespace pseudonym_hmac_key (≥{_MIN_PSEUDONYM_SECRET_BYTES} bytes)."
        )


def _pseudonym_token_suffix(entity_type: str, value: str, hmac_key: bytes | SecureKeyBuffer) -> str:
    """
    Deterministic opaque suffix: first 16 bytes of HMAC-SHA256, base64url-encoded.

    Yields ~22 characters (128 bits) vs the previous 64 hex chars (256 bits).
    Collision resistance: 2^64 (birthday bound) — adequate for pseudonyms
    within a single namespace (requires ~4 billion tokens before a collision
    becomes likely).

    Message binds entity type and raw value so types do not collide across categories.
    """
    msg = f"{entity_type}\x00{value}".encode()
    key_bytes = bytes(hmac_key) if isinstance(hmac_key, SecureKeyBuffer) else hmac_key
    raw = hmac.new(key_bytes, msg, hashlib.sha256).digest()
    # Truncate to 16 bytes (128 bits), encode as base64url without padding.
    return base64.urlsafe_b64encode(raw[:16]).rstrip(b"=").decode("ascii")


async def process(text: str, config: NamespacePIIConfig) -> PIIProcessResult:
    """
    Processes the text according to the namespace PII policy.
    """
    locale = getattr(config, "locale", "en")
    entities = await scan(text, config, locale=locale)

    if not entities:
        return PIIProcessResult(
            sanitized_text=text, redacted=False, entities_found=[], vault_entries=[]
        )

    if config.policy == PIIPolicy.reject:
        found_types = list(set(e.entity_type for e in entities))
        # Clear raw PII before raising so traceback frames never leak values.
        for entity in entities:
            entity.clear_raw_value()
        raise ValueError(
            f"PII Policy Reject: Found sensitive entities of type(s): {', '.join(found_types)}"
        )

    if config.policy == PIIPolicy.flag:
        # Clear raw PII from entities before returning — the caller may
        # hold the result in memory indefinitely (e.g. audit logs).
        for entity in entities:
            entity.clear_raw_value()
        return PIIProcessResult(
            sanitized_text=text,
            redacted=False,
            entities_found=list(set(e.entity_type for e in entities)),
            vault_entries=[],
        )

    # Redact or Pseudonymise
    sanitized_text = text
    vault_entries = []
    pseudonym_key_buf: SecureKeyBuffer | None = None
    if config.policy == PIIPolicy.pseudonymise:
        pseudonym_key_buf = _pseudonym_hmac_key_material(
            config, namespace_id=str(config.namespace_id)
        )

    from contextlib import nullcontext

    cm = require_master_key() if config.reversible else nullcontext()
    pm = pseudonym_key_buf if pseudonym_key_buf else nullcontext()

    replacement_triples: list[tuple[int, int, str]] = []

    with cm as mk, pm as pkb:
        for entity in entities:
            if entity.start < 0 or entity.end > len(sanitized_text) or entity.start > entity.end:
                for e in entities:
                    e.clear_raw_value()
                raise ValueError(
                    f"Invalid entity span ({entity.start}, {entity.end}) "
                    f"for text of length {len(sanitized_text)}"
                )

        for entity in entities:
            if config.policy == PIIPolicy.pseudonymise:
                assert pkb is not None
                digest = _pseudonym_token_suffix(
                    entity.entity_type,
                    entity.value,
                    bytes(pkb),
                )
                token = f"<{entity.entity_type}_{digest}>"

                if config.reversible:
                    # We reuse encrypt_signing_key as it provides AES-256-GCM encryption
                    encrypted_val = encrypt_signing_key(
                        entity.value.encode("utf-8"),
                        mk,  # type: ignore[arg-type]
                    )
                    vault_entries.append(
                        {
                            "token": token,
                            "encrypted_value": encrypted_val,
                            "entity_type": entity.entity_type,
                        }
                    )
            else:
                # Standard redact
                token = f"<{entity.entity_type}>"

            entity.token = token
            replacement_triples.append((entity.start, entity.end, token))
            entity.clear_raw_value()

        pieces: list[str] = []
        cursor = len(text)
        for start, end, token in sorted(replacement_triples, key=lambda x: x[0], reverse=True):
            pieces.append(text[end:cursor])
            pieces.append(token)
            cursor = start
        pieces.append(text[:cursor])
        sanitized_text = "".join(reversed(pieces))

    return PIIProcessResult(
        sanitized_text=sanitized_text,
        redacted=True,
        entities_found=list(set(e.entity_type for e in entities)),
        vault_entries=vault_entries,
    )
