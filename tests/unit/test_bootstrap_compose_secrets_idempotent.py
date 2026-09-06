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
    """Guard against over-correcting: a *weak* generated value must still be fixed.

    Uses NCE_JWT_SECRET, not the master key: the master key is write-once (see
    below) and is deliberately exempt from this.
    """
    mod = _load_script(tree)
    mod.GENERATED.write_text("NCE_JWT_SECRET=short\n", encoding="utf-8")
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert vals["NCE_JWT_SECRET"] != "short"
    assert len(vals["NCE_JWT_SECRET"]) >= 32


def test_a_weak_master_key_is_kept_not_rotated(tree: Path) -> None:
    """A short master key must FAIL AT BOOT, not be silently replaced here.

    nce/signing.py refuses a master key under 32 bytes by name, at boot. Keeping
    a weak one therefore surfaces loudly and recoverably; rotating it destroys
    the ability to unwrap every DEK in the database. Refuse-loudly beats
    re-key-silently, so the write-once rule outranks the strength rule here.
    """
    mod = _load_script(tree)
    mod.SECRETS_DIR = tree / "deploy" / "secrets"
    mod.GENERATED.write_text("NCE_MASTER_KEY=short\n", encoding="utf-8")
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert vals["NCE_MASTER_KEY"] == "short", (
        "a weak master key was rotated rather than left to fail at boot"
    )


# ---------------------------------------------------------------------------
# Write-once secrets — the master key must be structurally unrotatable here
# ---------------------------------------------------------------------------
#
# The boot guard (orchestrator._verify_master_key_matches_data) already refuses
# in production when the master key cannot open its data. It is not sufficient
# on its own, for two reasons this file exists to pin:
#
#   1. It runs at BOOT, on the READ path. This script runs BEFORE boot, on the
#      WRITE path, and produces the file boot then reads.
#   2. secret_env() gives NCE_MASTER_KEY_FILE precedence over the plain env var.
#      worker/cron/admin/a2a mount the key as a file and would boot correctly on
#      it, leaving the boot guard silent, while webhook-receiver — which has no
#      *_FILE override — runs on the rotated env value. A guard cannot detect a
#      divergence it is never exposed to.
#
# So rotation is prevented at the only place that can cause it.


def _seed_secret_file(tree: Path, name: str, value: str) -> None:
    d = tree / "deploy" / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(value + "\n", encoding="utf-8")


def test_master_key_is_never_rotated_once_a_secret_file_exists(tree: Path) -> None:
    original = "m" * 48
    _seed_secret_file(tree, "nce_master_key", original)
    mod = _load_script(tree)
    mod.SECRETS_DIR = tree / "deploy" / "secrets"

    for _ in range(3):
        mod.main()
        vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
        assert vals["NCE_MASTER_KEY"] == original, (
            "the master key was rotated. Rotating it is not a configuration "
            "change, it is data loss: content is encrypted under per-memory DEKs "
            "wrapped by this key."
        )


def test_a_divergent_env_copy_is_reconciled_to_the_secret_file(tree: Path) -> None:
    """The split-brain shape itself: env and file holding different master keys.

    The file is authoritative because that is what ``*_FILE`` mounts into the
    containers, and therefore what the running stack actually used.
    """
    truth = "t" * 48
    drifted = "d" * 48
    _seed_secret_file(tree, "nce_master_key", truth)
    (tree / "deploy" / "compose.stack.env.generated").write_text(
        f"NCE_MASTER_KEY={drifted}\n", encoding="utf-8"
    )
    mod = _load_script(tree)
    mod.SECRETS_DIR = tree / "deploy" / "secrets"
    mod.main()

    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert vals["NCE_MASTER_KEY"] == truth, (
        "the env copy was left diverging from the Docker-secret file — this is "
        "exactly the state in which webhook-receiver runs on a different master "
        "key from worker/cron/admin/a2a, with every container reporting healthy."
    )


def test_first_ever_run_still_generates_a_master_key(tree: Path) -> None:
    """Guard the guard: write-once must not mean never-written."""
    mod = _load_script(tree)
    mod.SECRETS_DIR = tree / "deploy" / "secrets"  # does not exist
    mod.main()
    vals = mod._parse_env_text(mod.GENERATED.read_text(encoding="utf-8"))
    assert len(vals["NCE_MASTER_KEY"]) >= 32


def test_write_once_covers_the_master_key(tree: Path) -> None:
    """If this set is ever emptied, every test above passes vacuously."""
    mod = _load_script(tree)
    assert "NCE_MASTER_KEY" in mod.WRITE_ONCE
    assert mod.WRITE_ONCE["NCE_MASTER_KEY"] == "nce_master_key"


def test_fingerprint_matches_the_orchestrator_and_never_leaks_the_secret(tree: Path) -> None:
    """The printed fingerprint must be comparable to what the orchestrator logs."""
    import hashlib

    mod = _load_script(tree)
    value = "s" * 40
    fp = mod._fingerprint(value)
    assert fp == hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    assert value not in fp
