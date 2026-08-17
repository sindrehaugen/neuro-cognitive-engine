"""
nce/assertion.py — Memory assertion-type classifier (§D9).

Extracted from pii.py (SRP violation: inference logic was living alongside PII scanning).
"""

from __future__ import annotations


def infer_assertion_type(text: str) -> str:
    """Rule-based classifier for memory assertion typing.

    Returns one of: ``"opinion"``, ``"preference"``, ``"observation"``, ``"fact"``.
    """
    text_lower = text.lower()
    if any(
        phrase in text_lower for phrase in ["i think", "i believe", "in my opinion", "seems like"]
    ):
        return "opinion"
    if any(
        phrase in text_lower for phrase in ["i prefer", "i like", "i love", "i hate", "favorite"]
    ):
        return "preference"
    if any(phrase in text_lower for phrase in ["i saw", "i noticed", "observed", "looks like"]):
        return "observation"
    return "observation"
