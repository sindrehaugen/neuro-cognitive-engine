"""
nce/vertical_modules/marketing/taxonomy.py
==========================================
Brand voice configuration, banned claims, required disclaimers, and content templates.

U4 Compliant: Config-as-code embedded in Python to honor the nce/config_data/**
freeze pending architectural governance rulings.
"""

from __future__ import annotations

from typing import Any

DEFAULT_BRAND_VOICE: dict[str, Any] = {
    "tone": "authoritative, technical, outcomes-focused, modern",
    "reading_level": "professional B2B engineering / executive",
    "banned_claims": [
        "100% bug-free",
        "zero latency",
        "unbreakable security",
        "guaranteed lowest price",
        "revolutionary magic",
    ],
    "required_disclaimers": [
        "Actual system performance may vary based on environmental acoustics, network architecture, and site conditions.",
        "Outcome metrics represent measured client results from verified post-commissioning surveys.",
    ],
    "anonymisation_rules": {
        "mask_client_name": True,
        "client_placeholder": "Nordic Enterprise Client",
        "mask_location": True,
        "location_placeholder": "Northern Europe",
        "mask_site_addresses": True,
    },
}

CASE_STUDY_SECTIONS: list[dict[str, str]] = [
    {
        "id": "challenge",
        "heading": "The Challenge",
        "guidance": "Summarize operational friction, latency, or room-system limitations prior to upgrade.",
    },
    {
        "id": "solution",
        "heading": "The Engineering Solution",
        "guidance": "Describe AV-over-IP, DSP topology, microphone coverage, and control architecture.",
    },
    {
        "id": "outcomes",
        "heading": "Verified Outcomes",
        "guidance": "Concrete metrics (meeting start speed, downtime reduction, support ticket drops).",
    },
    {
        "id": "room_narrative",
        "heading": "Room Experience",
        "guidance": "Spatial audio clarity, acoustic envelope, and seamless user interaction.",
    },
]
