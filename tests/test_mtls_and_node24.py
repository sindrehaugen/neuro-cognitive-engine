"""Tests for internal mTLS certificate expiry checks, Caddyfile/Compose/Makefile parity, and Node-24 workflows."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_internal_mtls.py"


def _load_mtls_module():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import generate_internal_mtls

        return generate_internal_mtls
    finally:
        sys.path.pop(0)


def _strip_comments_and_blank_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def test_generate_internal_mtls_check_expiry_days_cli_and_helper(tmp_path: Path) -> None:
    """--check-expiry-days exits 1 on missing/short certs and 0 when valid >= N days."""
    mod = _load_mtls_module()
    cert_dir = tmp_path / "certs"

    # Missing directory -> exit 1
    assert mod.main(["--out-dir", str(cert_dir), "--check-expiry-days", "14"]) == 1

    # Generate 30-day bundle
    mod.generate_internal_mtls_bundle(cert_dir, validity_days=30)
    ca_before = (cert_dir / "ca.crt").read_bytes()

    # Valid for >= 14 days -> exit 0 without overwriting existing certs
    assert mod.main(["--out-dir", str(cert_dir), "--check-expiry-days", "14"]) == 0
    assert (cert_dir / "ca.crt").read_bytes() == ca_before

    # Requires >= 45 days when bundle only has 30 days -> exit 1
    assert mod.main(["--out-dir", str(cert_dir), "--check-expiry-days", "45"]) == 1

    # Missing caddy.crt -> exit 1
    (cert_dir / "caddy.crt").unlink()
    assert mod.main(["--out-dir", str(cert_dir), "--check-expiry-days", "14"]) == 1

    # Subprocess CLI smoke check on clean 90-day bundle
    cli_dir = tmp_path / "cli_certs"
    mod.generate_internal_mtls_bundle(cli_dir, validity_days=90)
    ok_proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--out-dir", str(cli_dir), "--check-expiry-days", "30"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0, ok_proc.stdout + ok_proc.stderr

    fail_proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--out-dir", str(cli_dir), "--check-expiry-days", "120"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail_proc.returncode == 1


def test_caddyfile_mtls_parity_with_caddyfile() -> None:
    """deploy/Caddyfile.mtls keeps full route, body-limit, header, CSP, and vertical vars parity with Caddyfile."""
    base_raw = (REPO_ROOT / "Caddyfile").read_text(encoding="utf-8")
    mtls_raw = (REPO_ROOT / "deploy" / "Caddyfile.mtls").read_text(encoding="utf-8")

    # Required security headers & CSP parity
    for required in (
        "admin off",
        "auto_https disable_redirects",
        "http://:80, https://localhost:443, https://127.0.0.1:443",
        "tls internal",
        'X-Frame-Options "DENY"',
        'X-Content-Type-Options "nosniff"',
        'X-XSS-Protection "0"',
        'Referrer-Policy "strict-origin-when-cross-origin"',
        'Permissions-Policy "geolocation=(), camera=(), microphone=()"',
        'Content-Security-Policy "default-src \'self\'; script-src \'self\' \'unsafe-inline\' \'unsafe-eval\' https://cdn.jsdelivr.net; style-src \'self\' \'unsafe-inline\' https://fonts.googleapis.com; font-src \'self\' https://fonts.gstatic.com data:; img-src \'self\' data:; connect-src \'self\';"',
        "-Server",
        "max_size 20971520",
        "handle /webhooks/*",
        "max_size 1048576",
        "reverse_proxy webhook-receiver:8080",
        "@a2a_routes path /tasks/* /.well-known/agent-card",
        "reverse_proxy a2a:8004",
        "@mcp_routes path /mcp /mcp/*",
        "reverse_proxy mcp-gateway:8005",
        "flush_interval -1",
        'vars portal_upstream "portal:8010"',
        'vars odoo_upstream "odoo:8011"',
        'vars salesforce_upstream "salesforce:8012"',
        "handle /api/portal/*",
        "handle_path /portal/*",
        "handle_path /odoo/*",
        "handle_path /salesforce/*",
        "reverse_proxy {vars.portal_upstream}",
        "reverse_proxy {vars.odoo_upstream}",
        "reverse_proxy {vars.salesforce_upstream}",
        "handle /caddy-healthz",
        'respond "ok" 200',
    ):
        assert required in base_raw, f"Missing {required!r} in Caddyfile"
        assert required in mtls_raw, f"Missing {required!r} in deploy/Caddyfile.mtls"

    # Non-admin directives up to `/caddy-healthz` must match Caddyfile identically
    base_prefix = base_raw.split("handle /caddy-healthz", 1)[0]
    mtls_prefix = mtls_raw.split("handle /caddy-healthz", 1)[0]
    assert _strip_comments_and_blank_lines(mtls_prefix) == _strip_comments_and_blank_lines(
        base_prefix
    )

    # Admin upstream in Caddyfile.mtls must enforce mTLS SAN + cert pool
    assert "reverse_proxy https://admin:8003" in mtls_raw
    assert "header_up X-Client-Cert-SAN caddy.internal.nce" in mtls_raw
    assert "tls_trust_pool file /etc/nce/certs/ca.crt" in mtls_raw
    assert "tls_client_auth /etc/nce/certs/caddy.crt /etc/nce/certs/caddy.key" in mtls_raw


def test_docker_compose_mtls_env_parity_and_makefile_targets() -> None:
    """docker-compose.mtls.yml maintains prod/secret parity and Makefile exposes mtls-certs and up-mtls."""
    mtls_compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.mtls.yml").read_text(encoding="utf-8")
    )
    admin_env = mtls_compose["services"]["admin"]["environment"]

    assert admin_env["NCE_ENV"] == "${NCE_ENV:-prod}"
    assert admin_env["NCE_LOAD_DOTENV"] == "false"
    assert admin_env["NCE_ADMIN_OVERRIDE"] == "false"
    assert admin_env["NCE_DISABLE_MIGRATION_MCP"] == "${NCE_DISABLE_MIGRATION_MCP:-true}"
    assert admin_env["NCE_A2A_JWT_AUDIENCE"] == "${NCE_A2A_JWT_AUDIENCE:-nce-a2a}"
    assert admin_env["NCE_ADMIN_MTLS_ENABLED"] == "true"
    assert admin_env["NCE_ADMIN_MTLS_STRICT"] == "true"
    assert admin_env["NCE_MTLS_STRICT"] == "true"
    assert admin_env["NCE_ADMIN_MTLS_ALLOWED_SANS"] == "caddy.internal.nce"
    assert admin_env["NCE_MASTER_KEY_FILE"] == "/run/secrets/nce_master_key"
    assert admin_env["NCE_API_KEY_FILE"] == "/run/secrets/nce_api_key"
    assert admin_env["NCE_MASTER_KEY"] == ""
    assert admin_env["NCE_API_KEY"] == ""

    makefile_text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"(?m)^mtls-certs:", makefile_text)
    assert re.search(r"(?m)^up-mtls:\s*mtls-certs\b", makefile_text)
    assert "--check-expiry-days" in makefile_text
    assert "docker-compose.mtls.yml" in makefile_text


def test_workflows_have_no_deprecated_node20_actions() -> None:
    """All Core workflows (and sibling vertical workflows if present) avoid deprecated @v2 release/node20 actions."""
    workflow_dirs = [REPO_ROOT / ".github" / "workflows"]
    verticals_wf = REPO_ROOT.parent / "nce-vertical-engines" / ".github" / "workflows"
    if verticals_wf.is_dir():
        workflow_dirs.append(verticals_wf)

    deprecated_patterns = (
        "softprops/action-gh-release@v2",
        "actions/checkout@v4",
        "actions/checkout@v5",
        "actions/setup-python@v5",
        "actions/setup-go@v5",
        "actions/upload-artifact@v4",
        "actions/configure-pages@v5",
        "actions/upload-pages-artifact@v3",
        "actions/deploy-pages@v4",
    )

    checked = 0
    for wf_dir in workflow_dirs:
        for wf in sorted(wf_dir.glob("*.yml")):
            checked += 1
            text = wf.read_text(encoding="utf-8")
            for bad in deprecated_patterns:
                assert bad not in text, f"Deprecated Node-20 action {bad!r} found in {wf}"

    assert checked >= 8
