#!/usr/bin/env python3
"""Helper for scripts/render-env.sh — Jinja render of client-env-template.j2 from terraform output JSON."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from jinja2 import Template
except ImportError:
    print("Install jinja2:  python3 -m pip install jinja2", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    if len(sys.argv) != 4:
        print(
            "Usage: _render_env.py <template.j2> <outputs.json> <cloud>",
            file=sys.stderr,
        )
        sys.exit(2)
    template_path = Path(sys.argv[1])
    raw = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    cloud = sys.argv[3]

    def val(key: str, default: str = "") -> str:
        block = raw.get(key)
        if isinstance(block, dict) and "value" in block:
            v = block["value"]
            return "" if v is None else str(v)
        return default

    ctx = {
        "cloud": cloud,
        "deployment_name": val("deployment_name", os.environ.get("TRIMCP_DEPLOYMENT_NAME", "")),
        "region": val("region", os.environ.get("TRIMCP_REGION", "")),
        "postgres_secret_ref": val("postgres_secret_arn")
        or val("postgres_secret_id")
        or val("postgres_secret_name"),
        "mongo_secret_ref": val("mongo_secret_arn")
        or val("documentdb_secret_arn")
        or val("mongo_connection_secret_id"),
        "redis_secret_ref": val("redis_secret_arn")
        or val("redis_auth_secret_arn")
        or val("redis_secret_id"),
        "postgres_host": val("postgres_endpoint_address")
        or val("postgres_host")
        or val("postgres_private_ip"),
        "postgres_port": val("postgres_port", "5432"),
        "postgres_database": val("postgres_database_name", "memory_meta"),
        "mongo_hosts": val("documentdb_endpoint") or val("mongo_hosts"),
        "mongo_database": val("mongo_database_name", "nce"),
        "redis_host": val("redis_primary_endpoint") or val("redis_host"),
        "redis_port": val("redis_port", "6379"),
        "blob_endpoint": val("blob_endpoint")
        or val("s3_bucket_regional_domain_name")
        or ("https://storage.googleapis.com" if val("gcs_bucket_name") else ""),
        "blob_bucket": val("blob_bucket_name") or val("s3_bucket_id") or val("gcs_bucket_name"),
        "webhook_public_base_url": val("webhook_public_base_url")
        or val("webhook_invoke_url")
        or val("webhook_public_url", "https://REPLACE_ME"),
    }
    tpl = Template(template_path.read_text(encoding="utf-8"))
    print(tpl.render(**ctx))


if __name__ == "__main__":
    main()
