"""
nce/vertical_modules/hr/taxonomy.py
===================================
Standard AV/IT skills taxonomy and certification mappings for Module 13.

Embeds domain taxonomy in code to respect the nce/config_data/** freeze (Charter U4).
Covers major AV manufacturer certifications (CTS, Crestron, QSC, Biamp, Cisco, Dante)
and resolves implied skills.
"""

from __future__ import annotations

from typing import Any

# Standard AV skills taxonomy grouped by technical domain
_SKILLS_TAXONOMY: dict[str, dict[str, Any]] = {
    "audio": {
        "description": "Commercial and architectural audio systems",
        "skills": [
            "DSP programming",
            "Acoustic calibration",
            "Microphone array setup",
            "Dante routing",
            "AES67 configuration",
            "Speaker voicing",
        ],
    },
    "video": {
        "description": "Video distribution, presentation, and display walls",
        "skills": [
            "AV-over-IP streaming",
            "EDID management",
            "LED wall alignment",
            "PTZ camera tracking",
            "Video conferencing codecs",
        ],
    },
    "control": {
        "description": "Room automation and unified control systems",
        "skills": [
            "Crestron SIMPL/C#",
            "Q-SYS control scripting",
            "Extron Global Configurator",
            "Touch panel UI design",
            "RS-232/IP device driver development",
        ],
    },
    "network": {
        "description": "Enterprise AV networking and infrastructure",
        "skills": [
            "Multicast IGMP configuration",
            "VLAN segmentation",
            "QoS for real-time media",
            "PTP clocking synchronization",
            "Network switch commissioning",
        ],
    },
    "infrastructure": {
        "description": "Field installation and physical plant",
        "skills": [
            "Structured cabling",
            "Fiber optic fusion splicing",
            "Rack build & dressing",
            "Cable termination & testing",
            "Site commissioning",
            "AV System Design",
            "Q-SYS Core Commissioning",
        ],
    },
}

# Certifications and the technical competencies they formally imply
_CERT_IMPLICATIONS: dict[str, list[str]] = {
    "CTS": ["Site commissioning", "Cable termination & testing", "Structured cabling"],
    "CTS-D": ["AV System Design", "Site commissioning", "EDID management"],
    "CTS-I": ["Rack build & dressing", "Cable termination & testing", "Structured cabling"],
    "Crestron Certified Programmer": ["Crestron SIMPL/C#", "Touch panel UI design"],
    "Crestron DM-NVX": ["AV-over-IP streaming", "EDID management", "Multicast IGMP configuration"],
    "Q-SYS Level 1": ["DSP programming", "Audio routing"],
    "Q-SYS Level 2": ["Q-SYS Core Commissioning", "DSP programming", "Q-SYS control scripting"],
    "Biamp Tesira Forte": ["DSP programming", "Acoustic calibration"],
    "Dante Level 1": ["Dante routing"],
    "Dante Level 2": ["Dante routing", "AES67 configuration"],
    "Dante Level 3": ["Dante routing", "PTP clocking synchronization", "QoS for real-time media"],
    "Cisco Webex Specialist": ["Video conferencing codecs", "Network switch commissioning"],
}


def get_skill_taxonomy() -> dict[str, dict[str, Any]]:
    """Return the active skills taxonomy."""
    return dict(_SKILLS_TAXONOMY)


def get_cert_taxonomy() -> dict[str, list[str]]:
    """Return certification implications."""
    return dict(_CERT_IMPLICATIONS)


def resolve_implied_skills(certs: list[str]) -> set[str]:
    """Given a list of certification names, resolve all formally implied skills."""
    implied: set[str] = set()
    for cert in certs:
        # Match case-insensitively or exactly
        for cert_key, skills in _CERT_IMPLICATIONS.items():
            if cert.strip().lower() == cert_key.lower():
                implied.update(skills)
    return implied
