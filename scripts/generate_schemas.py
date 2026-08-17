"""Generate a combined JSON Schema document from all public Pydantic models.

Usage (from repo root):
    python scripts/generate_schemas.py [--out docs/schemas.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path so nce is importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

os.environ.setdefault("NCE_MASTER_KEY", "dev-schema-key-32chars-long-xxxx")


def build_schema() -> dict:
    """Return the combined JSON Schema for all public NCE API models."""
    from pydantic.json_schema import models_json_schema

    from nce.models import (
        ForgetMemoryRequest,
        GetRecentContextRequest,
        GraphSearchRequest,
        IndexCodeFileRequest,
        KGEdge,
        KGNode,
        ManageQuotasRequest,
        MediaPayload,
        MemoryRecord,
        NamespaceCognitiveConfig,
        NamespaceCreate,
        NamespaceMetadata,
        NamespaceMetadataPatch,
        NamespacePIIConfig,
        NamespaceRecord,
        SemanticSearchRequest,
        SemanticSearchResult,
        StoreMemoryRequest,
        UnredactMemoryRequest,
    )

    _PUBLIC_MODELS = [
        NamespaceCreate,
        NamespaceRecord,
        NamespaceMetadata,
        NamespaceMetadataPatch,
        NamespaceCognitiveConfig,
        NamespacePIIConfig,
        ManageQuotasRequest,
        StoreMemoryRequest,
        MemoryRecord,
        ForgetMemoryRequest,
        UnredactMemoryRequest,
        GetRecentContextRequest,
        SemanticSearchRequest,
        SemanticSearchResult,
        GraphSearchRequest,
        IndexCodeFileRequest,
        KGNode,
        KGEdge,
        MediaPayload,
    ]

    _, schema = models_json_schema(
        [(m, "validation") for m in _PUBLIC_MODELS],
        title="NCE API Schema",
    )
    return schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate NCE JSON Schema")
    parser.add_argument(
        "--out",
        default="docs/schemas.json",
        help="Output path (default: docs/schemas.json)",
    )
    args = parser.parse_args()

    schema = build_schema()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Schema written to {out_path} ({len(schema.get('$defs', {}))} models)")


if __name__ == "__main__":
    main()
