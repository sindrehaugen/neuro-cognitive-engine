"""
dep_report.py — NCE Dependency Audit Reporter

Generates a markdown dependency health report from pip-audit and pip outdated
output, and optionally emails it to administrators via aiosmtplib.

Usage:
    # Generate report only
    python scripts/dep_report.py --audit-json /tmp/audit.json \
        --outdated-json /tmp/outdated.json --output report.md

    # Generate + send email (requires NCE_SMTP_* env vars)
    python scripts/dep_report.py --audit-json /tmp/audit.json \
        --outdated-json /tmp/outdated.json --send-email

    # Run interactively (generates fresh data, no pre-computed JSON needed)
    python scripts/dep_report.py --live --send-email

Environment variables for email:
    NCE_SMTP_HOST     SMTP server hostname
    NCE_SMTP_PORT     SMTP port (default: 587)
    NCE_SMTP_USER     SMTP username
    NCE_SMTP_PASS     SMTP password
    NCE_ADMIN_EMAIL   Comma-separated list of recipient addresses
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def load_json(path: str) -> dict | list:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def run_pip_audit() -> list:
    """Run pip-audit and return parsed JSON results."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip_audit",
            "-r",
            "requirements.txt",
            "--format=json",
            "--disable-pip",
        ],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout).get("dependencies", [])
    except json.JSONDecodeError:
        return []


def run_outdated() -> list:
    """Run pip list --outdated and return parsed JSON results."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def build_report(audit_data: list | dict, outdated_data: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# NCE Dependency Health Report",
        f"**Generated:** {now}",
        "",
    ]

    # --- CVE / Vulnerability section ----------------------------------------
    # pip-audit returns either a list of deps or {"dependencies": [...]}
    if isinstance(audit_data, dict):
        deps = audit_data.get("dependencies", [])
    else:
        deps = audit_data

    vulnerable = [d for d in deps if d.get("vulns")]

    if vulnerable:
        lines += [
            f"## 🔴 Security Vulnerabilities ({len(vulnerable)} packages affected)",
            "",
            "| Package | Installed | CVE | Description | Fix |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]
        for dep in vulnerable:
            name = dep.get("name", "?")
            version = dep.get("version", "?")
            for vuln in dep.get("vulns", []):
                vid = vuln.get("id", "?")
                desc = vuln.get("description", "")[:80].replace("|", "/")
                fix = vuln.get("fix_versions", ["—"])
                fix_str = ", ".join(fix) if fix else "—"
                lines.append(f"| `{name}` | {version} | {vid} | {desc} | {fix_str} |")
        lines.append("")
    else:
        lines += ["## ✅ No Known Vulnerabilities", ""]

    # --- Outdated packages section ------------------------------------------
    # Filter to packages that appear in requirements.txt
    try:
        with open("requirements.txt") as f:
            req_names = {
                line.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
                for line in f
                if line.strip() and not line.startswith("#")
            }
    except FileNotFoundError:
        req_names = set()

    relevant_outdated = [
        p
        for p in outdated_data
        if p.get("name", "").lower().replace("-", "_") in {n.replace("-", "_") for n in req_names}
        or p.get("name", "").lower() in req_names
    ]

    if relevant_outdated:
        lines += [
            f"## 🟡 Outdated Direct Dependencies ({len(relevant_outdated)} packages)",
            "",
            "| Package | Current | Latest | Type |",
            "| :--- | :--- | :--- | :--- |",
        ]

        # Sort: major bumps first, then minor
        def bump_severity(p: dict) -> int:
            cur = p.get("version", "0").split(".")
            lat = p.get("latest_version", "0").split(".")
            try:
                if int(lat[0]) > int(cur[0]):
                    return 0  # major — highest priority
                if len(lat) > 1 and len(cur) > 1 and int(lat[1]) > int(cur[1]):
                    return 1  # minor
            except (ValueError, IndexError):
                pass
            return 2  # patch

        for pkg in sorted(relevant_outdated, key=bump_severity):
            name = pkg.get("name", "?")
            cur = pkg.get("version", "?")
            lat = pkg.get("latest_version", "?")
            typ = pkg.get("latest_filetype", "wheel")
            lines.append(f"| `{name}` | {cur} | {lat} | {typ} |")
        lines.append("")
    else:
        lines += ["## ✅ All Direct Dependencies Up To Date", ""]

    # --- Summary footer -----------------------------------------------------
    vuln_count = len(vulnerable)
    outdated_count = len(relevant_outdated)
    status = (
        "🔴 ACTION REQUIRED"
        if vuln_count
        else ("🟡 Review recommended" if outdated_count > 5 else "✅ Healthy")
    )

    lines += [
        "---",
        f"**Status:** {status}  ",
        f"**Vulnerabilities:** {vuln_count}  ",
        f"**Outdated direct deps:** {outdated_count}  ",
        "",
        "_To update: edit minimum versions in `requirements.txt`, then run_",
        "_`uv pip compile requirements.txt -o requirements.lock && uv sync`_",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------


async def send_email(subject: str, body: str) -> None:
    try:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        import aiosmtplib
    except ImportError:
        print("aiosmtplib not installed — skipping email notification.", file=sys.stderr)
        return

    host = os.environ.get("NCE_SMTP_HOST")
    port = int(os.environ.get("NCE_SMTP_PORT", "587"))
    user = os.environ.get("NCE_SMTP_USER")
    password = os.environ.get("NCE_SMTP_PASS")
    recipients_raw = os.environ.get("NCE_ADMIN_EMAIL", "")

    if not all([host, user, password, recipients_raw]):
        print("SMTP config incomplete — skipping email notification.", file=sys.stderr)
        return

    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain"))

    await aiosmtplib.send(
        msg,
        hostname=host,
        port=port,
        username=user,
        password=password,
        start_tls=True,
    )
    print(f"Email notification sent to: {', '.join(recipients)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="NCE Dependency Audit Reporter")
    parser.add_argument("--audit-json", help="Path to pip-audit JSON output")
    parser.add_argument("--outdated-json", help="Path to pip list --outdated JSON output")
    parser.add_argument("--output", help="Write markdown report to this file")
    parser.add_argument("--send-email", action="store_true", help="Send email notification")
    parser.add_argument("--live", action="store_true", help="Run pip-audit and pip outdated fresh")
    args = parser.parse_args()

    if args.live:
        print("Running live dependency checks...")
        audit_data = run_pip_audit()
        outdated_data = run_outdated()
    else:
        audit_data = load_json(args.audit_json) if args.audit_json else []
        outdated_data = load_json(args.outdated_json) if args.outdated_json else []

    report = build_report(audit_data, outdated_data)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report written to {args.output}")
    else:
        print(report)

    if args.send_email:
        # Determine subject severity
        if isinstance(audit_data, dict):
            deps = audit_data.get("dependencies", [])
        else:
            deps = audit_data
        vuln_count = sum(1 for d in deps if d.get("vulns"))
        subject = (
            f"🔴 NCE Security Alert — {vuln_count} vulnerability(s) found"
            if vuln_count
            else "🟡 NCE Weekly Dependency Report"
        )
        asyncio.run(send_email(subject, report))


if __name__ == "__main__":
    main()
