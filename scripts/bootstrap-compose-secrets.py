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
SECRETS_DIR = ROOT / "deploy" / "secrets"

# Secrets this script may NEVER rotate once a value exists anywhere, and the
# Docker-secret file that is authoritative for each.
#
# Rotating a master key is not a configuration change, it is data loss: content
# is encrypted under per-memory DEKs wrapped by this key (nce.envelope), which
# is the same property that lets the system prove deletion. The boot guard added
# in the key-durability work (orchestrator._verify_master_key_matches_data)
# catches a wrong key in milliseconds — but it runs at BOOT, on the READ path,
# and this script runs BEFORE boot on the WRITE path. Worse, secret_env() gives
# NCE_MASTER_KEY_FILE precedence over the env var, so worker/cron/admin/a2a would
# have booted on the correct file-mounted key and the guard would have stayed
# silent, while webhook-receiver — which has no *_FILE override — ran on the
# rotated one. Detection at boot cannot see a divergence it is not exposed to.
#
# So the rule is enforced here, at the only place that can write a new one.
WRITE_ONCE: dict[str, str] = {
    "NCE_MASTER_KEY": "nce_master_key",
    "NCE_API_KEY": "nce_api_key",
}

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
            # _hash_pbkdf2 writes the DOUBLED form ($$pbkdf2$$...): a single $ in a
            # compose env file is interpolation, so the hash has to be escaped on
            # disk. This predicate only accepted the single-$ form, so the hash the
            # script itself had just written always failed its own strength check —
            # NCE_ADMIN_PASSWORD was guaranteed to rotate on every single run,
            # independently of the idempotency bug in main(). Accept both.
            or not (v.startswith("$pbkdf2$") or v.startswith("$$pbkdf2$$"))
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


def _fingerprint(value: str) -> str:
    """The same sha256[:16] the orchestrator logs, so the two can be compared.

    Never the secret itself: this is printed to a terminal and to CI logs.
    """
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


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


def _write_once_value(key: str, merged: dict[str, str]) -> str | None:
    """The value a write-once secret must keep, or ``None`` if there is none yet.

    Authority order: the Docker-secret file under ``deploy/secrets/`` first,
    because that is what ``*_FILE`` mounts into the containers and therefore what
    the running stack actually used; then any value already in the generated env
    file. If the two disagree, the file wins and the env copy is corrected --
    that divergence IS the split-brain, and leaving it in place is what let one
    service run on a different master key from the other four.
    """
    fname = WRITE_ONCE.get(key)
    if fname:
        path = SECRETS_DIR / fname
        if path.is_file():
            value = path.read_bytes().rstrip(b"\r\n").decode("utf-8").strip()
            if value:
                return value
    existing = merged.get(key, "").strip()
    return existing or None


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

    existing_gen = {}
    if GENERATED.is_file():
        existing_gen = _parse_env_text(GENERATED.read_text(encoding="utf-8"))

    merged: dict[str, str] = {k: v for k, v in existing_gen.items() if not k.startswith("#")}

    # 🔴 A key is generated ONLY when the base file's value is weak AND the
    # generated file does not already hold a strong one.
    #
    # This loop used to generate for every base-weak key and then assign into
    # `merged` unconditionally, which overwrote the values the line above had
    # just preserved. The docstring said "Idempotent"; the code rotated EVERY
    # secret on EVERY run, NCE_MASTER_KEY included. `make up` calls this before
    # `docker compose up`, so running `make up` twice re-keyed the stack.
    #
    # The blast radius is not symmetric. Four services take the master key via
    # NCE_MASTER_KEY_FILE, which wins over the env var, but webhook-receiver has
    # no *_FILE override and reads NCE_MASTER_KEY straight from this file — so a
    # rotation here gives one service a different master key from the other four
    # while every container still reports healthy. That is the exact shape of the
    # 2026-09-02 outage in which the deployed stack could not decrypt its own
    # active signing key for 26 hours.
    for key, is_weak in KEY_SPECS:
        if key in WRITE_ONCE:
            # Write-once: never rotate, and always reconcile the env copy to the
            # Docker-secret file so the two can never drift apart.
            keep = _write_once_value(key, merged)
            if keep is not None:
                if merged.get(key) != keep:
                    print(
                        f"{key}: kept the existing value "
                        f"(fingerprint {_fingerprint(keep)}); "
                        "write-once secrets are never rotated by this script."
                    )
                merged[key] = keep
                continue
            if not is_weak(base_vals.get(key, "")):
                continue  # operator supplied a real value in the base file
            merged[key] = _gen_for_key(key)
            print(f"{key}: GENERATED FOR THE FIRST TIME — record it in escrow now.")
            continue
        if not is_weak(base_vals.get(key, "")):
            continue  # base file carries a real value; nothing to override
        if key in merged and not is_weak(merged[key]):
            continue  # a strong generated value already exists — keep it
        merged[key] = _gen_for_key(key)

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
