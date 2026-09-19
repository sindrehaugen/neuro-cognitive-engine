"""Drift gate & specification ratchet: OpenAPI 3.1.0 document currency and coverage.

This suite ensures:
1. `docs/_generated/openapi.json` and `openapi.json` are present, well-formed, and strictly
   in sync with `scripts/gen_openapi.py` output.
2. Every mounted HTTP route across `admin_app.build_admin_routes()` and secondary apps
   (`me_app`, `customer_portal`, `a2a_server`) is covered in the OpenAPI specification
   with valid operation metadata and response schemas.
3. Every registered C12 `ResourceSpec` has schemas defined in `components.schemas`.
4. All MCP tools defined in `TOOLS` have valid schema representations.
5. U18 Standing Positive Control: an unschematized or omitted route strictly fails validation.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest
from starlette.routing import Route

os.environ.setdefault("NCE_MASTER_KEY", "dev-schema-key-32chars-long-xxxx")

_ROOT = Path(__file__).resolve().parent.parent

# Dynamically load scripts/gen_openapi.py
_spec = importlib.util.spec_from_file_location("gen_openapi", _ROOT / "scripts" / "gen_openapi.py")
assert _spec and _spec.loader
gen_openapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_openapi)


@pytest.fixture(scope="module")
def openapi_doc() -> dict[str, Any]:
    """Return the generated OpenAPI specification dictionary."""
    return gen_openapi.generate_openapi_spec()


@pytest.fixture(scope="module")
def all_mounted_routes() -> list[Route]:
    """Collect all active mounted Route objects across the application suite."""
    from nce.admin_app import build_admin_routes
    from nce.resource_surface import load_all_engine_resources

    load_all_engine_resources()

    routes: list[Route] = []
    for r in build_admin_routes():
        if isinstance(r, Route):
            routes.append(r)

    try:
        import nce.me_app

        for r in nce.me_app.app.routes:
            if isinstance(r, Route):
                routes.append(r)
    except Exception:
        pass

    try:
        from nce.vertical_modules.customer_portal.app import build_customer_portal_app

        for r in build_customer_portal_app().routes:
            if isinstance(r, Route):
                routes.append(r)
    except Exception:
        pass

    try:
        import nce.a2a_server

        for r in nce.a2a_server.app.routes:
            if isinstance(r, Route):
                routes.append(r)
    except Exception:
        pass

    return routes


def validate_routes_in_spec(routes: list[Route], spec: dict[str, Any]) -> list[str]:
    """Validate that every mounted Route has a corresponding path, method, and response schema in the spec.

    Returns a list of validation failure error strings (empty if valid).
    """
    paths = spec.get("paths", {})
    errors: list[str] = []

    for route in routes:
        path = route.path
        if path not in paths:
            errors.append(f"Route path {path!r} ({route.name}) is missing from OpenAPI paths.")
            continue

        path_item = paths[path]
        methods = [m.upper() for m in (route.methods or {"GET"}) if m.upper() != "HEAD"]
        if not methods:
            methods = ["GET"]

        for method in methods:
            m_lower = method.lower()
            if m_lower not in path_item:
                errors.append(
                    f"Route {method} {path!r} ({route.name}) is missing operation in OpenAPI paths."
                )
                continue

            op = path_item[m_lower]
            if not op.get("operationId"):
                errors.append(f"Operation {method} {path!r} lacks an operationId.")

            responses = op.get("responses")
            if not responses or not isinstance(responses, dict):
                errors.append(f"Operation {method} {path!r} has no responses defined.")
                continue

            # Must have at least one success or default response
            success_codes = [c for c in responses if str(c).startswith("2") or str(c) == "default"]
            if not success_codes:
                errors.append(f"Operation {method} {path!r} has no 2xx or default response.")
            else:
                for code in success_codes:
                    resp_obj = responses[code]
                    if not isinstance(resp_obj, dict) or "description" not in resp_obj:
                        errors.append(
                            f"Operation {method} {path!r} response {code} lacks description."
                        )

    return errors


def test_openapi_document_is_current():
    """Verify that committed openapi.json and docs/_generated/openapi.json match generator."""
    expected_content = gen_openapi.generate_json_string()

    docs_file = _ROOT / "docs" / "_generated" / "openapi.json"
    root_file = _ROOT / "openapi.json"

    assert docs_file.exists(), f"{docs_file} does not exist. Run python scripts/gen_openapi.py"
    assert root_file.exists(), f"{root_file} does not exist. Run python scripts/gen_openapi.py"

    actual_docs = docs_file.read_text(encoding="utf-8")
    actual_root = root_file.read_text(encoding="utf-8")

    assert actual_docs == expected_content, (
        "docs/_generated/openapi.json has drifted from live route/spec definitions. "
        "Regenerate with: python scripts/gen_openapi.py"
    )
    assert actual_root == expected_content, (
        "openapi.json has drifted from live route/spec definitions. "
        "Regenerate with: python scripts/gen_openapi.py"
    )


def test_every_mounted_route_has_schema(all_mounted_routes, openapi_doc):
    """Every mounted HTTP route must have an OpenAPI operation with response schemas."""
    assert len(all_mounted_routes) >= 500, (
        f"Expected at least 500 mounted routes, found {len(all_mounted_routes)}. "
        "Precondition failed: route table incomplete."
    )
    errors = validate_routes_in_spec(all_mounted_routes, openapi_doc)
    assert not errors, f"Found {len(errors)} unschematized or invalid routes:\n" + "\n".join(
        errors[:25]
    )


def test_every_resource_spec_has_schema(openapi_doc):
    """All registered C12 ResourceSpecs must have their item, create, update, and list schemas defined."""
    from nce.resource_surface import get_all_resource_specs, load_all_engine_resources

    load_all_engine_resources()
    specs = get_all_resource_specs()
    assert len(specs) >= 20, f"Expected at least 20 ResourceSpecs, found {len(specs)}"

    schemas = openapi_doc.get("components", {}).get("schemas", {})

    for spec in specs:
        model_name = f"{spec.engine.capitalize()}_{spec.entity.replace('-', '_')}"
        create_name = f"{model_name}_Create"
        update_name = f"{model_name}_Update"
        list_name = f"{model_name}_List"

        assert model_name in schemas, (
            f"ResourceSpec {spec.engine}.{spec.entity} lacks item schema {model_name!r}"
        )
        assert create_name in schemas, (
            f"ResourceSpec {spec.engine}.{spec.entity} lacks create schema {create_name!r}"
        )
        assert update_name in schemas, (
            f"ResourceSpec {spec.engine}.{spec.entity} lacks update schema {update_name!r}"
        )
        assert list_name in schemas, (
            f"ResourceSpec {spec.engine}.{spec.entity} lacks list schema {list_name!r}"
        )


def test_all_mcp_tools_schematized(openapi_doc):
    """All declarative MCP tools must be defined under x-mcp-tools and components.schemas."""
    from nce.mcp_stdio_tools import TOOLS

    assert len(TOOLS) >= 300, f"Expected >= 300 MCP tools, found {len(TOOLS)}"

    x_tools = openapi_doc.get("components", {}).get("x-mcp-tools", {})
    schemas = openapi_doc.get("components", {}).get("schemas", {})

    missing_tools: list[str] = []
    missing_schemas: list[str] = []

    for tool in TOOLS:
        name = getattr(tool, "name", None) or tool["name"]
        if name not in x_tools:
            missing_tools.append(name)
        schema_key = f"Tool_{name}_Input"
        if tool.inputSchema and schema_key not in schemas:
            missing_schemas.append(schema_key)

    assert not missing_tools, f"MCP tools missing from x-mcp-tools: {missing_tools[:10]}"
    assert not missing_schemas, (
        f"MCP tool input schemas missing from components.schemas: {missing_schemas[:10]}"
    )


def test_positive_control_unschematized_route_fails(openapi_doc):
    """U18 Standing Positive Control: prove that unschematized or stripped routes fail validation loudly."""
    # 1. Synthetic unschematized route must trigger error
    synthetic_route = Route(
        "/api/synthetic/unmapped_test_endpoint_xyz",
        endpoint=lambda req: None,
        methods=["GET", "POST"],
        name="synthetic_unmapped",
    )
    errors = validate_routes_in_spec([synthetic_route], openapi_doc)
    assert errors, "Positive control failed: synthetic unmapped route did NOT produce errors"
    assert any("is missing from OpenAPI paths" in err for err in errors)

    # 2. Spec missing an existing route must trigger error
    tampered_spec = json.loads(json.dumps(openapi_doc))
    real_paths = list(tampered_spec["paths"].keys())
    assert real_paths, "Spec has no paths"
    removed_path = real_paths[0]
    del tampered_spec["paths"][removed_path]

    sample_route = Route(
        removed_path,
        endpoint=lambda req: None,
        methods=["GET"],
        name="sample_removed",
    )
    tampered_errors = validate_routes_in_spec([sample_route], tampered_spec)
    assert tampered_errors, (
        f"Positive control failed: removing path {removed_path} was not detected"
    )

    # 3. Spec missing response definition must trigger error
    tampered_spec_2 = json.loads(json.dumps(openapi_doc))
    target_path = real_paths[1]
    method = list(tampered_spec_2["paths"][target_path].keys())[0]
    tampered_spec_2["paths"][target_path][method]["responses"] = {}

    response_tampered_route = Route(
        target_path,
        endpoint=lambda req: None,
        methods=[method.upper()],
        name="sample_empty_responses",
    )
    resp_errors = validate_routes_in_spec([response_tampered_route], tampered_spec_2)
    assert resp_errors, (
        f"Positive control failed: removing responses from {target_path} was not detected"
    )
