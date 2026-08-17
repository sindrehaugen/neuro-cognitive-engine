"""Pure-function entity normalizers — no DB/HTTP access.

Loads namespace-specific normalization maps from JSON config files
and provides a single entry point to normalize values by namespace.
"""

import json
from pathlib import Path

# In-process cache: {normalizer_name: alias_map}
_NORMALIZER_CACHE: dict[str, dict[str, str]] = {}


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
        value: The value to normalize (e.g. 'Cisco Systems', 'CISCO')
        name: The normalizer name (e.g. 'manufacturer')

    Returns:
        str: The normalized value (casefolded + stripped, then aliased if found).
             If the value is not in the alias map, returns the casefolded + stripped
             form (no error on unknown value).
    """
    # Casefold and strip
    normalized = value.strip().casefold()

    # Load the alias map
    alias_map = load_normalizer(name)

    # Return aliased value if found, else the casefolded form
    return alias_map.get(normalized, normalized)
