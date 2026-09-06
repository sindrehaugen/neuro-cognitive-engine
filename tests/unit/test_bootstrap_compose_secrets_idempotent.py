"""``scripts/bootstrap-compose-secrets.py`` must be idempotent.

The script's own docstring has always claimed "Idempotent: only replaces keys
that still look weak compared to compose.stack.env." It was not. ``main()``
generated a fresh value for every key whose *base-file* value was weak, then
assigned each one into ``merged`` — overwriting the previously generated strong
values the line above had just preserved. Every run rotated every secret,
``NCE_MASTER_KEY`` included.

``make up`` runs this script and then ``docker compose up``, so running
``make up`` a second time re-keyed the whole stack.

The blast radius is asymmetric, which is what made it survivable long enough to
go unnoticed: ``worker``, ``cron``, ``admin`` and ``a2a`` receive the master key
through ``NCE_MASTER_KEY_FILE``, and ``*_FILE`` wins over the plain env var, so
those four keep working. ``webhook-receiver`` has no ``*_FILE`` override and
reads ``NCE_MASTER_KEY`` directly from the generated env file — so a rotation
hands one service a different master key from the other four while every
container still reports healthy. That is the 2026-09-02 outage's exact shape:
26 hours of a deployed stack unable to decrypt its own active signing key.

These tests run the script's real ``main()`` against a temporary tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap-compose-secrets.py"

# A base env whose values are all weak placeholders — the condition under which
# the script generates anything at all.
_WEAK_BASE = "\n".join(
    [
        "NCE_MASTER_KEY=dev-master-key",
        "NCE_API_KEY=dev-api-key",
        "NCE_ADMIN_API_KEY=dev-admin-key",
        "NCE_MCP_API_KEY=dev-mcp-key",
        "NCE_JWT_SECRET=dev",
        "NCE_ADMIN_PASSWORD=dev",
        "NCE_APP_PASSWORD=dev",
        "DROPBOX_APP_SECRET=dev-x",
        "GRAPH_CLIENT_STATE=dev-x",
        "DRIVE_CHANNEL_TOKEN=dev-x",
        "",
    ]
)


def _load_script(tmp_root: Path):
    """Import the script fresh with ROOT/BASE_ENV/GENERATED pointed at ``tmp_root``."""
    spec = importlib.util.spec_from_file_location(f"_bootstrap_{tmp_root.name}", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_root
    mod.BASE_ENV = tmp_root / "deploy" / "compose.stack.env"
    mod.EXAMPLE_ENV = tmp_root / "deploy" / "compose.stack.env.example"
    mod.GENERATED = tmp_root / "deploy" / "compose.stack.env.generated"
    return mod


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "compose.stack.env").write_text(_WEAK_BASE, encoding="utf-8")
    return tmp_path


def test_first_run_generates_strong_values(tree: Path) -> None:
    """Guard the guard: if the first run generated nothing, the test below is vacuous."""
    mod = _load_script(tree)
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert "NCE_MASTER_KEY" in vals, "the script generated nothing — the fixture is wrong"
    assert len(vals["NCE_MASTER_KEY"]) >= 32
    assert len(vals) >= 8


def test_second_run_rotates_nothing(tree: Path) -> None:
    """The regression. A re-run must leave every generated secret byte-identical."""
    mod = _load_script(tree)
    mod.main()
    first = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))

    mod.main()
    second = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))

    rotated = sorted(k for k in first if first[k] != second.get(k))
    assert not rotated, (
        f"{len(rotated)} secret(s) rotated on a second run: {rotated}. "
        "make up runs this script before docker compose up, so this re-keys a "
        "live stack. NCE_MASTER_KEY rotating here splits webhook-receiver "
        "(plain env var) from the four services that take it via "
        "NCE_MASTER_KEY_FILE, and every container still reports healthy."
    )
    assert first == second


def test_a_real_value_in_the_base_file_is_never_overridden(tree: Path) -> None:
    """A strong base value means the operator supplied it; do not shadow it."""
    mod = _load_script(tree)
    mod.main()
    strong = "a" * 64
    mod.BASE_ENV.write_text(
        _WEAK_BASE.replace("NCE_MASTER_KEY=dev-master-key", f"NCE_MASTER_KEY={strong}"),
        encoding="utf-8",
    )
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    # The stale generated override is preserved rather than regenerated, and no
    # NEW value is invented for a key the base file now answers.
    assert "NCE_MASTER_KEY" in vals
    mod.main()
    again = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert vals == again


def test_a_weak_generated_value_is_replaced(tree: Path) -> None:
    """Guard against over-correcting: a *weak* generated value must still be fixed."""
    mod = _load_script(tree)
    mod.GENERATED.write_text("NCE_MASTER_KEY=short\n", encoding="utf-8")
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert vals["NCE_MASTER_KEY"] != "short"
    assert len(vals["NCE_MASTER_KEY"]) >= 32
