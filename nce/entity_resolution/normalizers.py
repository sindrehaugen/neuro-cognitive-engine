"""Pure-function entity normalizers — no DB/HTTP access.

Loads namespace-specific normalization maps from JSON config files
and provides a single entry point to normalize values by namespace.
"""

import json
import re
from pathlib import Path

# In-process cache: {normalizer_name: alias_map}
_NORMALIZER_CACHE: dict[str, dict[str, str]] = {}


def normalize_org_nr(value: str) -> str:
    """Normalize an organization number (pure function, no DB/HTTP access).

    Strips whitespace, punctuation (dots, hyphens, slashes), spaces,
    leading 'NO' country code prefix, and trailing 'MVA' VAT suffixes.
    For standard Norwegian business registration numbers, produces a
    clean 9-digit string. For international/alphanumeric org numbers,
    produces a clean alphanumeric string.

    Args:
        value: Raw organization number (e.g. 'NO 987 654 321 MVA', '987-654-321', '987654321').

    Returns:
        str: Normalized organization number string.
    """
    if not value:
        return ""

    raw = value.strip()
    # Strip optional leading 'NO' (case-insensitive) if followed by digits/whitespace
    cleaned = re.sub(r"^(?:NO\s*|\bNO\b)", "", raw, flags=re.IGNORECASE).strip()
    # Strip optional trailing 'MVA' (case-insensitive)
    cleaned = re.sub(r"(?:\s*MVA|\bMVA\b)$", "", cleaned, flags=re.IGNORECASE).strip()
    # Remove all whitespace, hyphens, periods, slashes
    cleaned = re.sub(r"[\s\-\.\/]", "", cleaned)

    return cleaned


def load_normalizer(name: str) -> dict[str, str]:
    """Load and return a normalizer alias map from config JSON.

    Pure function: reads from JSON file (nce/config_data/{name}-normalization.json)
    and caches in-process. No DB or HTTP access.

    Args:
        name: The normalizer name (e.g. 'manufacturer'). Looks for
              nce/config_data/{name}-normalization.json

    Returns:
        dict[str, str]: A dictionary mapping original values to their normalized forms.
                       Empty dict if file doesn't exist or is empty.

    Raises:
        json.JSONDecodeError: If the JSON file is malformed.
    """
    # Check cache first
    if name in _NORMALIZER_CACHE:
        return _NORMALIZER_CACHE[name]

    config_path = Path(__file__).parent.parent / "config_data" / f"{name}-normalization.json"

    alias_map: dict[str, str] = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    alias_map = data
        except (OSError, json.JSONDecodeError):
            # Return empty map on file read/parse error
            # (silent degradation: unknown value passes through casefolded)
            pass

    _NORMALIZER_CACHE[name] = alias_map
    return alias_map


def normalize(value: str, name: str) -> str:
    """Normalize a value against a named normalizer map.

    Pure function: casefolds, strips whitespace, then applies aliases from
    the normalizer map. Unknown values pass through (casefolded, stripped).
    No DB or HTTP access.

    Args:
        value: The value to normalize (e.g. 'Cisco Systems', 'CISCO', '987 654 321')
        name: The normalizer name (e.g. 'manufacturer', 'org_nr', 'orgnr')

    Returns:
        str: The normalized value (casefolded + stripped, then aliased if found).
             If the value is not in the alias map, returns the casefolded + stripped
             form (no error on unknown value).
    """
    # Special-case org_nr / orgnr normalization
    if name in ("org_nr", "orgnr", "organization_number"):
        return normalize_org_nr(value)

    # Casefold and strip
    normalized = value.strip().casefold()

    # Load the alias map
    alias_map = load_normalizer(name)

    # Return aliased value if found, else the casefolded form
    return alias_map.get(normalized, normalized)
