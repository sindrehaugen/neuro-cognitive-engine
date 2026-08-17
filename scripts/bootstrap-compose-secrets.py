#!/usr/bin/env python3
"""
Generate strong secrets for Docker Compose when stack env still uses dev placeholders.

Writes deploy/compose.stack.env.generated (loaded after deploy/compose.stack.env)
so production values override weak defaults without editing the tracked base file.

Idempotent: only replaces keys that still look weak compared to compose.stack.env.

Scope (VI.1): this is the **development / single-host** secrets path. Production
deployments should source secrets from a real manager (HashiCorp Vault, AWS
Secrets Manager, Azure Key Vault) injected into the container environment via
the secrets-provider seam (NCE_SECRETS_PROVIDER / nce.config.resolve_secret),
rather than generating them into a local env file. NCE_MASTER_KEY is always
environment / secret-manager only and is never read from a database or
SettingsStore (R3). See deploy/README.md "Secrets management (production)".
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = ROOT / "deploy" / "compose.stack.env"
EXAMPLE_ENV = ROOT / "deploy" / "compose.stack.env.example"
GENERATED = ROOT / "deploy" / "compose.stack.env.generated"

HEADER = """# AUTO-GENERATED — do not commit real secrets. Added by scripts/bootstrap-compose-secrets.py
# Overrides entries from deploy/compose.stack.env when values there are weak placeholders.
"""

KEY_SPECS: list[tuple[str, Callable[[str], bool]]] = [
    (
        "NCE_MASTER_KEY",
        lambda v: (
            _weak(v, min_len=32)
            or "dev" in v.lower()
            or "change" in v.lower()
            or "replace_me" in v.lower()
        ),
    ),
    (
        "NCE_API_KEY",
        lambda v: (
            _weak(v, min_len=16)
            or "change" in v.lower()
            or v.lower().startswith("dev-")
            or "replace_me" in v.lower()
        ),
    ),
    (
        "NCE_ADMIN_API_KEY",
        lambda v: (
            _weak(v, min_len=16)
            or "change" in v.lower()
            or v.lower().startswith("dev-")
            or "replace_me" in v.lower()
        ),
    ),
    (
        "NCE_MCP_API_KEY",
        lambda v: (
            _weak(v, min_len=16)
            or "change" in v.lower()
            or v.lower().startswith("dev-")
            or "replace_me" in v.lower()
        ),
    ),
    (
        "NCE_APP_PASSWORD",
        lambda v: (
            _weak(v, min_len=8)
            or "change" in v.lower()
            or "replace_me" in v.lower()
            or v == "nce_app_secret"
        ),
    ),
    (
        "NCE_JWT_SECRET",
        lambda v: _weak(v, min_len=32) or "dev-jwt" in v.lower() or "replace_me" in v.lower(),
    ),
    (
        "NCE_ADMIN_PASSWORD",
        lambda v: (
            _weak(v, min_len=8)
            or v.lower() in ("changeme", "admin", "password")
            or "replace_me" in v.lower()
            or not v.startswith("$pbkdf2$")
        ),
    ),
    (
        "DROPBOX_APP_SECRET",
        lambda v: _weak(v) or v.lower().startswith("dev-") or "replace_me" in v.lower(),
    ),
    (
        "GRAPH_CLIENT_STATE",
        lambda v: _weak(v) or v.lower().startswith("dev-") or "replace_me" in v.lower(),
    ),
    (
        "DRIVE_CHANNEL_TOKEN",
        lambda v: _weak(v) or v.lower().startswith("dev-") or "replace_me" in v.lower(),
    ),
]


def _weak(v: str, min_len: int = 8) -> bool:
    if v is None:
        return True
    s = v.strip()
    return len(s) < min_len


def _parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, rest = line.partition("=")
        k = k.strip()
        out[k] = rest.strip().strip('"').strip("'")
    return out


def _hash_pbkdf2(password: str) -> str:
    import hashlib
    import os

    iters = 600000
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iters,
        dklen=32,
    )
    return f"$$pbkdf2$${iters}$${salt.hex()}$${dk.hex()}"


def _gen_for_key(key: str) -> str:
    if key == "NCE_ADMIN_PASSWORD":
        raw_pass = secrets.token_urlsafe(18)
        hashed_pass = _hash_pbkdf2(raw_pass)
        print("=" * 60)
        print(" NCE ADMINISTRATOR PASSWORD GENERATED ")
        print(f" Plaintext Password:  {raw_pass}")
        print(" Keep this password secure! It will NOT be written to disk in plaintext.")
        print(" Only the PBKDF2 hash is written to deploy/compose.stack.env.generated.")
        print("=" * 60)
        return hashed_pass
    if key == "NCE_APP_PASSWORD":
        return secrets.token_urlsafe(16)
    return secrets.token_hex(32)


def _ensure_base_env() -> None:
    if BASE_ENV.is_file():
        return
    if EXAMPLE_ENV.is_file():
        BASE_ENV.write_text(EXAMPLE_ENV.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created {BASE_ENV.name} from {EXAMPLE_ENV.name}")
        return
    raise SystemExit(f"Missing {BASE_ENV} and {EXAMPLE_ENV}")


def main() -> None:
    _ensure_base_env()

    base_vals = _parse_env_text(BASE_ENV.read_text(encoding="utf-8"))
    overrides: dict[str, str] = {}
    for key, is_weak in KEY_SPECS:
        cur = base_vals.get(key, "")
        if is_weak(cur):
            overrides[key] = _gen_for_key(key)

    existing_gen = {}
    if GENERATED.is_file():
        existing_gen = _parse_env_text(GENERATED.read_text(encoding="utf-8"))

    # Preserve previously generated strong values unless base file was fixed
    merged: dict[str, str] = {k: v for k, v in existing_gen.items() if not k.startswith("#")}
    for k, v in overrides.items():
        merged[k] = v

    if not merged:
        stub = HEADER + "\n# No weak secrets detected; nothing to generate.\n"
        GENERATED.write_text(stub, encoding="utf-8")
        print(f"Wrote {GENERATED} (no overrides needed).")
        return

    lines = [HEADER.rstrip(), ""]
    for k in sorted(merged.keys()):
        lines.append(f"{k}={merged[k]}")
    lines.append("")
    GENERATED.write_text("\n".join(lines), encoding="utf-8")
    print(f"Updated {GENERATED} with {len(merged)} secret override(s).")


if __name__ == "__main__":
    main()
