"""tests/unit/test_config_docs_ratchet.py
======================================
Wave I-10: Config Docs Default Value Ratchet.

Invariant:
Documented configuration defaults across docs/ must strictly match runtime declarations
in nce/config.py. Any documented default that cannot be resolved against a declared
config setting must be explicitly allowlisted with a substantive reason (>= 60 chars)
under KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS. Silent skipping is prohibited.

Notations supported:
- Notation A (docs/architecture-v1.md):
  `NAME`, def N[, min M]
- Notation B (docs/engines/product-admin.md):
  *   **`NAME`** ... *Default:* `N` (bullet-bounded)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Final

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR: Final[Path] = _REPO_ROOT / "docs"
_CONFIG_PATH: Final[Path] = _REPO_ROOT / "nce" / "config.py"

# Regex for Notation A: `NAME`, def N[, min M]
_NOTATION_A_RE: Final[re.Pattern[str]] = re.compile(
    r"`([A-Z0-9_]+)`,\s*def\s+([0-9]+(?:\.[0-9]+)?)"
)

# Regex for Notation B: top-level list items starting with *   **`NAME`**
_NOTATION_B_ITEM_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\n(?=\*\s+\*\*`[A-Z0-9_]+`\*\*)")
_NOTATION_B_VAR_RE: Final[re.Pattern[str]] = re.compile(r"^\*\s+\*\*`([A-Z0-9_]+)`\*\*")
_NOTATION_B_DEFAULT_RE: Final[re.Pattern[str]] = re.compile(r"\*\s+\*Default:\*\s+`([^`]+)`")

# ---------------------------------------------------------------------------
# Shrink-only allowlist for documented defaults that do not resolve cleanly
# to an _int_env / _bool_env / _float_env / _str_env declaration in nce/config.py.
# Every entry requires an owner, reason (>= 60 chars), source_file, and documented_default.
# ---------------------------------------------------------------------------
KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS: Final[dict[str, dict[str, Any]]] = {
    "BRIDGE_CRON_INTERVAL_MINUTES": {
        "owner": "core-bridge",
        "source_file": "docs/architecture-v1.md",
        "documented_default": 60,
        "reason": (
            "Documented as 'def 60' in architecture-v1.md, but declared in nce/config.py "
            "as raw os.getenv fallback 45 rather than through standard _int_env."
        ),
    },
    "REEMBED_CRON_INTERVAL_MINUTES": {
        "owner": "core-memory",
        "source_file": "docs/architecture-v1.md",
        "documented_default": 60,
        "reason": (
            "Documented as 'def 60' in architecture-v1.md, but declared in nce/config.py "
            "via compound max(1, int(os.getenv(...))) rather than standard _int_env."
        ),
    },
    "CONSOLIDATION_CRON_INTERVAL_MINUTES": {
        "owner": "core-memory",
        "source_file": "docs/architecture-v1.md",
        "documented_default": 360,
        "reason": (
            "Documented as 'def 360' in architecture-v1.md, but declared in nce/config.py "
            "via raw int(os.getenv(...)) rather than standard _int_env."
        ),
    },
    "OUTBOX_RELAY_INTERVAL_SECONDS": {
        "owner": "core-events",
        "source_file": "docs/architecture-v1.md",
        "documented_default": 5,
        "reason": (
            "Documented as 'def 5' in architecture-v1.md, but declared in nce/config.py "
            "via compound max(1, int(os.getenv(...))) rather than standard _int_env."
        ),
    },
    "DECAY_PRUNE_INTERVAL_MINUTES": {
        "owner": "core-memory",
        "source_file": "docs/architecture-v1.md",
        "documented_default": 60,
        "reason": (
            "Documented as a configurable default in architecture-v1.md, but implemented as a "
            "hardcoded constant in nce/temporal_decay.py:57 with no config.py entry."
        ),
    },
    "NCE_PRODUCT_SYNC_BATCH_SIZE": {
        "owner": "product",
        "source_file": "docs/engines/product-admin.md",
        "documented_default": 2000,
        "reason": (
            "Documented in product-admin.md as default 2000, but read locally via os.getenv "
            "in nce/vertical_modules/product/sources/nettailer.py rather than config.py."
        ),
    },
    "NCE_PRODUCT_HTTP_TIMEOUT": {
        "owner": "product",
        "source_file": "docs/engines/product-admin.md",
        "documented_default": 30.0,
        "reason": (
            "Documented in product-admin.md as default 30.0, but read locally via os.getenv "
            "in nce/vertical_modules/product/sources/nettailer.py rather than config.py."
        ),
    },
    "NCE_PRODUCT_ENRICH_MIN_CONFIDENCE": {
        "owner": "product",
        "source_file": "docs/engines/product-admin.md",
        "documented_default": 0.70,
        "reason": (
            "Documented in product-admin.md as default 0.70, but read locally via os.getenv "
            "in nce/vertical_modules/product/enrich.py rather than config.py."
        ),
    },
    "NCE_PRODUCT_EOL_WARN_DAYS": {
        "owner": "product",
        "source_file": "docs/engines/product-admin.md",
        "documented_default": 60,
        "reason": (
            "Documented in product-admin.md as default 60; the corresponding EOL signal tier is "
            "unimplemented and variable is not declared in nce/config.py."
        ),
    },
}


def _parse_scalar_value(raw: str) -> int | float | str:
    """Parse string representation of documented default into typed scalar."""
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _extract_notation_a_defaults(text: str, source_file: str) -> list[dict[str, Any]]:
    """Extract documented config defaults matching Notation A (`NAME`, def N)."""
    results: list[dict[str, Any]] = []
    for match in _NOTATION_A_RE.finditer(text):
        var_name = match.group(1)
        raw_val = match.group(2)
        results.append(
            {
                "var_name": var_name,
                "documented_default": _parse_scalar_value(raw_val),
                "notation": "A",
                "source_file": source_file,
            }
        )
    return results


def _extract_notation_b_defaults(text: str, source_file: str) -> list[dict[str, Any]]:
    """Extract documented config defaults matching Notation B (bullet-bounded)."""
    results: list[dict[str, Any]] = []
    # Split text into candidate chunks starting at top-level bullet *   **`NAME`**
    chunks = _NOTATION_B_ITEM_SPLIT_RE.split(text)
    for chunk in chunks:
        var_match = _NOTATION_B_VAR_RE.search(chunk)
        if not var_match:
            continue
        var_name = var_match.group(1)
        def_match = _NOTATION_B_DEFAULT_RE.search(chunk)
        if def_match:
            raw_val = def_match.group(1).strip()
            results.append(
                {
                    "var_name": var_name,
                    "documented_default": _parse_scalar_value(raw_val),
                    "notation": "B",
                    "source_file": source_file,
                }
            )
    return results


def _collect_all_documented_defaults(docs_root: Path) -> list[dict[str, Any]]:
    """Scan documentation directory for all documented configuration defaults."""
    all_defaults: list[dict[str, Any]] = []
    for doc_file in sorted(docs_root.rglob("*.md")):
        try:
            content = doc_file.read_text(encoding="utf-8")
        except Exception:
            continue
        rel_path = doc_file.relative_to(_REPO_ROOT).as_posix()
        all_defaults.extend(_extract_notation_a_defaults(content, rel_path))
        all_defaults.extend(_extract_notation_b_defaults(content, rel_path))
    return all_defaults


def _extract_declared_config_defaults(config_file: Path) -> dict[str, Any]:
    """Parse nce/config.py AST and extract declared defaults for _int_env, _bool_env, etc."""
    tree = ast.parse(config_file.read_bytes())
    declared: dict[str, Any] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("_int_env", "_bool_env", "_float_env", "_str_env"):
                if len(node.args) >= 2:
                    env_arg = node.args[0]
                    default_arg = node.args[1]
                    if isinstance(env_arg, ast.Constant) and isinstance(env_arg.value, str):
                        env_name = env_arg.value
                        if isinstance(default_arg, ast.Constant):
                            declared[env_name] = default_arg.value
                        elif isinstance(default_arg, ast.UnaryOp) and isinstance(
                            default_arg.op, ast.USub
                        ):
                            if isinstance(default_arg.operand, ast.Constant):
                                declared[env_name] = -default_arg.operand.value
    return declared


# ---------------------------------------------------------------------------
# Test Suite
# ---------------------------------------------------------------------------


def test_documented_defaults_match_config_declarations() -> None:
    """Every paired documented default must match its nce/config.py declaration exactly."""
    doc_defaults = _collect_all_documented_defaults(_DOCS_DIR)
    cfg_defaults = _extract_declared_config_defaults(_CONFIG_PATH)

    mismatches: list[str] = []
    compared_count = 0

    for item in doc_defaults:
        name = item["var_name"]
        doc_val = item["documented_default"]
        source = item["source_file"]

        if name in cfg_defaults:
            cfg_val = cfg_defaults[name]
            compared_count += 1
            if doc_val != cfg_val:
                mismatches.append(
                    f"{name} in {source}: documented={doc_val} ({type(doc_val).__name__}) "
                    f"!= config.py declared={cfg_val} ({type(cfg_val).__name__})"
                )

    assert not mismatches, (
        f"Documented config default mismatches found ({len(mismatches)}): {mismatches}. "
        "Update nce/config.py or docs/ so documented defaults agree with runtime code."
    )


def test_all_unpaired_documented_defaults_are_allowlisted() -> None:
    """Every documented default not found in nce/config.py must be allowlisted.

    Prohibits silent skipping: any documented setting absent from _int_env/_bool_env/...
    in config.py must be explicitly recorded with a rationale in KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS.
    """
    doc_defaults = _collect_all_documented_defaults(_DOCS_DIR)
    cfg_defaults = _extract_declared_config_defaults(_CONFIG_PATH)

    unaccounted: list[str] = []
    for item in doc_defaults:
        name = item["var_name"]
        if name not in cfg_defaults:
            if name not in KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS:
                unaccounted.append(
                    f"{name} ({item['source_file']}: documented default {item['documented_default']})"
                )

    assert not unaccounted, (
        f"Found {len(unaccounted)} documented defaults not declared in nce/config.py and "
        f"not in KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS: {unaccounted}. "
        "Either declare the setting in nce/config.py or allowlist it with a substantive rationale."
    )


def test_unpaired_allowlist_is_shrink_only_and_reasoned() -> None:
    """KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS must be shrink-only and possess >= 60 char reasons."""
    doc_defaults = _collect_all_documented_defaults(_DOCS_DIR)
    doc_names = {item["var_name"] for item in doc_defaults}

    for name, entry in KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS.items():
        assert name in doc_names, (
            f"Allowlist entry '{name}' is not present in docs/. "
            "Remove stale allowlist entry from KNOWN_UNPAIRED_DOCUMENTED_DEFAULTS."
        )

        owner = entry.get("owner")
        assert owner and isinstance(owner, str), f"Allowlist entry '{name}' missing owner."

        reason = entry.get("reason")
        assert reason and isinstance(reason, str), f"Allowlist entry '{name}' missing reason."
        assert len(reason.strip()) >= 60, (
            f"Allowlist entry '{name}' reason too brief ({len(reason.strip())} chars < 60): {reason}"
        )

        source_file = entry.get("source_file")
        assert source_file and isinstance(source_file, str), (
            f"Allowlist entry '{name}' missing source_file."
        )


def test_discovery_floors_for_doc_notations() -> None:
    """Guard-the-guard: verify extractor discovers documented defaults and compares pairs.

    Measured baselines across repository:
    - Notation A: 12 documented defaults in docs/architecture-v1.md (floor >= 8)
    - Notation B: 4 documented defaults in docs/engines/product-admin.md (floor >= 3)
    - Total documented defaults: 16 (floor >= 12)
    - Pairs compared against nce/config.py: 7 (floor >= 5)
    """
    doc_defaults = _collect_all_documented_defaults(_DOCS_DIR)
    cfg_defaults = _extract_declared_config_defaults(_CONFIG_PATH)

    notation_a = [d for d in doc_defaults if d["notation"] == "A"]
    notation_b = [d for d in doc_defaults if d["notation"] == "B"]
    compared = [d for d in doc_defaults if d["var_name"] in cfg_defaults]

    assert len(notation_a) >= 8, (
        f"Discovery floor breached for Notation A: expected >= 8, found {len(notation_a)}"
    )
    assert len(notation_b) >= 3, (
        f"Discovery floor breached for Notation B: expected >= 3, found {len(notation_b)}"
    )
    assert len(doc_defaults) >= 12, (
        f"Discovery floor breached for total documented defaults: expected >= 12, found {len(doc_defaults)}"
    )
    assert len(compared) >= 5, (
        f"Discovery floor breached for compared pairs: expected >= 5, found {len(compared)}"
    )


# ---------------------------------------------------------------------------
# Standing Positive Controls (U18)
# ---------------------------------------------------------------------------


def test_positive_control_fails_on_mismatched_doc_default() -> None:
    """Standing positive control: verify scanner catches a synthetic doc default mismatch."""
    synthetic_doc = (
        "| **7** | `d365_entity_sync` | Every $D$ min (`NCE_D365_SYNC_INTERVAL_MINUTES`, def 999) |"
    )
    extracted = _extract_notation_a_defaults(synthetic_doc, "synthetic_doc.md")
    assert len(extracted) == 1
    assert extracted[0]["var_name"] == "NCE_D365_SYNC_INTERVAL_MINUTES"
    assert extracted[0]["documented_default"] == 999

    cfg_defaults = _extract_declared_config_defaults(_CONFIG_PATH)
    actual_cfg_val = cfg_defaults["NCE_D365_SYNC_INTERVAL_MINUTES"]  # 60
    assert extracted[0]["documented_default"] != actual_cfg_val


def test_positive_control_does_not_cross_pair_adjacent_bullets() -> None:
    """Standing positive control: verify Notation B does not bleed across list items.

    Ensures a variable without a default (or with a different default) does not
    cross-pair with the *Default:* tag of an adjacent bullet item.
    """
    synthetic_doc = """
*   **`VAR_WITHOUT_DEFAULT`** (String)
    *   *Description:* An API URL without a default.
*   **`VAR_WITH_DEFAULT`** (Integer)
    *   *Description:* Chunk count.
    *   *Default:* `42` (min: `1`).
*   **`ANOTHER_VAR_WITHOUT_DEFAULT`** (Boolean)
    *   *Description:* An opt-in toggle.
"""
    extracted = _extract_notation_b_defaults(synthetic_doc, "synthetic_admin.md")
    assert len(extracted) == 1, f"Expected exactly 1 extracted default, got {len(extracted)}"
    assert extracted[0]["var_name"] == "VAR_WITH_DEFAULT"
    assert extracted[0]["documented_default"] == 42


def test_positive_control_ignores_non_config_prose_numbers() -> None:
    """Standing positive control: verify scanner ignores ports, row counts, and status codes."""
    non_config_text = """
    Connecting to PostgreSQL on localhost:9000 or MinIO on port 9002.
    Executing HTTP GET /health returning HTTP 200 OK.
    The table contains 6,423 def test_ statements across 528 files.
    Daily (def 1440 min / 24h, min 60) without backticked variable name.
    """
    extracted_a = _extract_notation_a_defaults(non_config_text, "prose.md")
    extracted_b = _extract_notation_b_defaults(non_config_text, "prose.md")

    assert len(extracted_a) == 0, f"Expected 0 Notation A matches, got {extracted_a}"
    assert len(extracted_b) == 0, f"Expected 0 Notation B matches, got {extracted_b}"
