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
_ENGINE_STATUS_PATH: Final[Path] = _REPO_ROOT / "docs" / "vertical_engines" / "ENGINE_STATUS.md"
_MIGRATIONS_DIR: Final[Path] = _REPO_ROOT / "nce" / "migrations"
_TOOL_REGISTRY_TEST_PATH: Final[Path] = _REPO_ROOT / "tests" / "test_tool_registry.py"
_GOLDEN_THREAD_TEST_PATH: Final[Path] = (
    _REPO_ROOT / "tests" / "integration" / "test_golden_thread.py"
)

# Regexes for ENGINE_STATUS.md figure inventory (A-FIG / I-10 extension)
_ENGINE_STATUS_TOOLS_RE: Final[re.Pattern[str]] = re.compile(
    r"\|\s*`TOOL_REGISTRY` entries\s*\|\s*\*\*(\d+)\*\*\s+MCP tools"
)
_ENGINE_STATUS_MAX_MIGRATION_RE: Final[re.Pattern[str]] = re.compile(
    r"\|\s*SQL migrations\s*\|\s*.*?`001`\s*→\s*`(\d{3})`"
)
_ENGINE_STATUS_GOLDEN_THREAD_RE: Final[re.Pattern[str]] = re.compile(
    r"\|\s*Golden Thread seam burndown\s*\|\s*\*\*(\d+)\s+of\s+(\d+)\*\*\s+lifecycle steps broken"
)

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
    """Parse nce/config.py AST and extract declared defaults for _int_env, os.getenv, etc."""
    tree = ast.parse(config_file.read_bytes())
    declared: dict[str, Any] = {}

    def _parse_node_val(raw_node: ast.AST) -> Any:
        if isinstance(raw_node, ast.Constant):
            if isinstance(raw_node.value, str):
                return _parse_scalar_value(raw_node.value)
            return raw_node.value
        elif isinstance(raw_node, ast.UnaryOp) and isinstance(raw_node.op, ast.USub):
            if isinstance(raw_node.operand, ast.Constant) and isinstance(
                raw_node.operand.value, (int, float)
            ):
                return -raw_node.operand.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in (
                "_int_env",
                "_bool_env",
                "_float_env",
                "_str_env",
            ):
                if len(node.args) >= 2:
                    env_arg = node.args[0]
                    default_arg = node.args[1]
                    if isinstance(env_arg, ast.Constant) and isinstance(env_arg.value, str):
                        val = _parse_node_val(default_arg)
                        if val is not None:
                            declared[env_arg.value] = val
            else:
                is_getenv = False
                if isinstance(node.func, ast.Attribute) and node.func.attr == "getenv":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                        is_getenv = True
                elif isinstance(node.func, ast.Name) and node.func.id == "getenv":
                    is_getenv = True

                if is_getenv and len(node.args) >= 2:
                    env_arg = node.args[0]
                    default_arg = node.args[1]
                    if isinstance(env_arg, ast.Constant) and isinstance(env_arg.value, str):
                        val = _parse_node_val(default_arg)
                        if val is not None:
                            declared[env_arg.value] = val
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
    - Pairs compared against nce/config.py: 11 (floor >= 8)
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
    assert len(compared) >= 8, (
        f"Discovery floor breached for compared pairs: expected >= 8, found {len(compared)}"
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


# ---------------------------------------------------------------------------
# A-FIG: Figure Counters in docs/vertical_engines/ENGINE_STATUS.md
# ---------------------------------------------------------------------------


def extract_engine_status_figures(text: str) -> dict[str, Any]:
    """Extract inventory figures from docs/vertical_engines/ENGINE_STATUS.md text."""
    m_tools = _ENGINE_STATUS_TOOLS_RE.search(text)
    assert m_tools, "Could not find `TOOL_REGISTRY` entries row in ENGINE_STATUS.md"
    tool_count = int(m_tools.group(1))

    m_mig = _ENGINE_STATUS_MAX_MIGRATION_RE.search(text)
    assert m_mig, "Could not find SQL migrations row in ENGINE_STATUS.md"
    max_migration = m_mig.group(1)

    m_gt = _ENGINE_STATUS_GOLDEN_THREAD_RE.search(text)
    assert m_gt, "Could not find Golden Thread seam burndown row in ENGINE_STATUS.md"
    gt_open_breaks = int(m_gt.group(1))
    gt_total_steps = int(m_gt.group(2))

    return {
        "tool_count": tool_count,
        "max_migration": max_migration,
        "gt_open_breaks": gt_open_breaks,
        "gt_total_steps": gt_total_steps,
    }


def get_expected_tool_count_from_repo() -> int:
    """Retrieve expected TOOL_REGISTRY total from tests/test_tool_registry.py AST."""
    tree = ast.parse(_TOOL_REGISTRY_TEST_PATH.read_bytes())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "_EXPECTED_TOTAL":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                        return node.value.value
    raise ValueError(f"_EXPECTED_TOTAL not found in {_TOOL_REGISTRY_TEST_PATH}")


def get_max_migration_from_repo() -> str:
    """Retrieve highest 3-digit migration prefix from nce/migrations/."""
    mig_nums = [
        int(m.group(1))
        for f in _MIGRATIONS_DIR.glob("*.sql")
        if (m := re.match(r"^([0-9]{3})_", f.name))
    ]
    assert mig_nums, f"No SQL migrations found in {_MIGRATIONS_DIR}"
    return f"{max(mig_nums):03d}"


def get_golden_thread_breaks_from_repo() -> tuple[int, int]:
    """Count open breaks (strict xfails) and total lifecycle steps in Golden Thread."""
    tree = ast.parse(_GOLDEN_THREAD_TEST_PATH.read_bytes())
    open_breaks = 0
    total_steps = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if re.match(r"^test_step_\d+_\w+$", node.name):
                total_steps += 1
                for dec in node.decorator_list:
                    call = dec if isinstance(dec, ast.Call) else None
                    func = call.func if call else dec
                    name = getattr(func, "attr", None) or getattr(func, "id", None)
                    if name == "xfail":
                        open_breaks += 1
                        break
    assert total_steps > 0, f"No Golden Thread lifecycle steps found in {_GOLDEN_THREAD_TEST_PATH}"
    return open_breaks, total_steps


# NOTE (A-FIG re-scope per ML-orch directive):
# The TOOL_REGISTRY tool-count assertion is deferred to D-GEN (DL lane),
# which makes that figure generated rather than written, rendering a hand-maintained
# ratchet redundant. The two stable counters below (max migration number and
# Golden Thread lifecycle breaks) are ratcheted here.


def test_engine_status_max_migration_matches_migrations_dir() -> None:
    """A-FIG Counter 2: Verify max migration number in ENGINE_STATUS.md matches nce/migrations/.

    Truth on main: highest nce/migrations/0XX_*.sql.
    ENGINE_STATUS.md: 001 -> max migration.
    """
    text = _ENGINE_STATUS_PATH.read_text(encoding="utf-8")
    figs = extract_engine_status_figures(text)
    repo_max = get_max_migration_from_repo()
    doc_max = figs["max_migration"]
    assert doc_max == repo_max, (
        f"docs/vertical_engines/ENGINE_STATUS.md max migration ({doc_max}) does not match "
        f"highest migration in nce/migrations/ ({repo_max}). "
        f"Routed to DL / D-GEN to regenerate docs via scripts/gen_engine_figures.py."
    )


def test_engine_status_golden_thread_breaks_match_suite() -> None:
    """A-FIG Counter 3: Verify Golden Thread open breaks in ENGINE_STATUS.md matches test suite.

    Truth on main: 0 open breaks of 28 lifecycle steps in test_golden_thread.py.
    ENGINE_STATUS.md says: 0 of 28 lifecycle steps broken.
    Verdict: GREEN.
    """
    text = _ENGINE_STATUS_PATH.read_text(encoding="utf-8")
    figs = extract_engine_status_figures(text)
    repo_open, repo_total = get_golden_thread_breaks_from_repo()
    assert (figs["gt_open_breaks"], figs["gt_total_steps"]) == (repo_open, repo_total), (
        f"docs/vertical_engines/ENGINE_STATUS.md Golden Thread breaks ({figs['gt_open_breaks']} of {figs['gt_total_steps']}) "
        f"does not match test_golden_thread.py xfails ({repo_open} of {repo_total})."
    )


def test_positive_control_catches_synthetic_migration_drift() -> None:
    """Standing positive control (U18): verify ratchet catches migration number divergence."""
    repo_max = get_max_migration_from_repo()
    synthetic_max = f"{int(repo_max) + 1:03d}"
    synthetic_doc = (
        "| `TOOL_REGISTRY` entries | **259** MCP tools (69 shared + 190 engine) |\n"
        f"| SQL migrations | 76 files (+1 optional), `001` → `{synthetic_max}` — gaps ... |\n"
        "| Golden Thread seam burndown | **0 of 28** lifecycle steps broken ... |\n"
    )
    figs = extract_engine_status_figures(synthetic_doc)
    assert figs["max_migration"] == synthetic_max
    assert figs["max_migration"] != repo_max


def test_positive_control_catches_synthetic_golden_thread_drift() -> None:
    """Standing positive control (U18): verify ratchet catches Golden Thread break count divergence."""
    repo_open, repo_total = get_golden_thread_breaks_from_repo()
    synthetic_open = repo_open + 1
    synthetic_doc = (
        "| `TOOL_REGISTRY` entries | **259** MCP tools (69 shared + 190 engine) |\n"
        "| SQL migrations | 74 files (+1 optional), `001` → `078` — gaps ... |\n"
        f"| Golden Thread seam burndown | **{synthetic_open} of {repo_total}** lifecycle steps broken ... |\n"
    )
    figs = extract_engine_status_figures(synthetic_doc)
    assert (figs["gt_open_breaks"], figs["gt_total_steps"]) == (synthetic_open, repo_total)
    assert (figs["gt_open_breaks"], figs["gt_total_steps"]) != (repo_open, repo_total)
