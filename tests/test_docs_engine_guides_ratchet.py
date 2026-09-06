"""Docs-status ratchet (Wave I-6 / DL-Orch D-3).

Two assertions, both aimed at the same failure mode that motivated this charter:
``docs/vertical_engines/ENGINE_STATUS.md`` once said "135 MCP tools" and marked six
merged, running engines "Planned" for five days before anyone noticed, because
nothing in ``tests/`` checked it against the code.

1. Every package under ``nce/vertical_modules/`` that registers at least one MCP
   tool in ``TOOL_REGISTRY`` MUST have a ``docs/engines/<engine>-user.md`` guide.
   A merged engine with a registered tool and no guide is exactly the D-1 gap this
   charter closed (support, customer_portal, resources, assets, marketing, five of
   them, 2026-09-06) -- this test is the floor that keeps that count at zero.
2. ``docs/_generated/surface.md`` (the file ``ENGINE_STATUS.md`` tells readers to
   trust over its own numbers) is regenerated from the live tree, not hand-edited
   -- i.e. it matches what ``scripts/gen_surface_table.py`` produces right now.
   If this fails, someone edited the generated file directly instead of running
   the generator, and it has silently started to drift.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from nce.tool_registry import TOOL_REGISTRY

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_ENGINES_DIR = _ROOT / "docs" / "engines"
_SURFACE_DOC = _ROOT / "docs" / "_generated" / "surface.md"

# `dynamics365` and `netbox` are the charter's "supporting integrations", not one of the
# 17 numbered vertical engines -- they predate and use a different, older documentation
# convention (a single technical-reference doc under docs/, not a docs/engines/<slug>-user.md
# + -admin.md pair). Found 2026-09-06 when this ratchet's first run flagged both as "missing"
# -- they are not missing, they are named differently. Map them explicitly rather than
# silently widening the glob, so a REAL missing guide for either can't hide behind this line.
_ALTERNATE_GUIDE_LOCATIONS: dict[str, pathlib.Path] = {
    "dynamics365": _ROOT / "docs" / "d365_integration_reference.md",
    "netbox": _ROOT / "docs" / "netbox_and_cognitive_extensions.md",
}


def _load_generator():
    path = _ROOT / "scripts" / "gen_surface_table.py"
    spec = importlib.util.spec_from_file_location("gen_surface_table", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _engines_with_registered_tools() -> dict[str, int]:
    """Map engine slug -> registered tool count, by resolved handler module path.

    Mirrors ``gen_surface_table.extract_tools``'s engine-attribution logic (module
    path substring match against ``nce/vertical_modules/<engine>/``) so this test
    and the generator can never silently disagree about which package a tool
    belongs to.
    """
    gen = _load_generator()
    gen.VERTICAL_ENGINES = gen.discover_vertical_engines(str(_ROOT), "HEAD")
    tools = gen.extract_tools(str(_ROOT), "HEAD")
    counts: dict[str, int] = {}
    for tool in tools:
        eng = tool["engine"]
        if eng == "shared":
            continue
        counts[eng] = counts.get(eng, 0) + 1
    return counts


def test_every_tool_bearing_engine_has_a_user_guide():
    counts = _engines_with_registered_tools()
    assert counts, "no vertical-module tools found -- the discovery logic itself is broken"

    missing = []
    for engine, tool_count in sorted(counts.items()):
        if tool_count < 1:
            continue
        if engine in _ALTERNATE_GUIDE_LOCATIONS:
            alt = _ALTERNATE_GUIDE_LOCATIONS[engine]
            if not alt.exists():
                missing.append(
                    f"{engine} ({tool_count} tools) -> expected {alt.relative_to(_ROOT)} "
                    "(mapped alternate location, also missing)"
                )
            continue
        slug = engine.replace("_", "-")
        guide = _ENGINES_DIR / f"{slug}-user.md"
        if not guide.exists():
            missing.append(f"{engine} ({tool_count} tools) -> expected {guide.relative_to(_ROOT)}")

    assert not missing, (
        "engine(s) with registered MCP tools have no docs/engines/<engine>-user.md guide:\n  "
        + "\n  ".join(missing)
        + "\nWrite the guide (see docs/engines/economy-user.md or inventory-user.md as models) "
        "before landing more tools for this engine."
    )


def test_registered_tool_count_is_at_least_the_charter_floor():
    """Positive control (U18 pattern): the ratchet must be able to go RED.

    Confirms the counting logic isn't vacuously trivial by asserting a floor well
    below the measured total (213 at charter issue) -- if TOOL_REGISTRY were ever
    empty or the import broke, this would catch it rather than the emptiness
    silently satisfying an unconditional "no missing guides" pass above.
    """
    assert len(TOOL_REGISTRY) >= 100, (
        f"TOOL_REGISTRY has only {len(TOOL_REGISTRY)} entries -- far below the expected order of "
        "magnitude; the registry import likely broke rather than the estate having shrunk."
    )


@pytest.mark.skipif(not _SURFACE_DOC.exists(), reason="docs/_generated/surface.md not present")
def test_generated_surface_doc_matches_the_generator():
    gen = _load_generator()
    gen.VERTICAL_ENGINES = gen.discover_vertical_engines(str(_ROOT), "HEAD")
    all_engines = list(gen.VERTICAL_ENGINES) + ["shared"]
    engine_data = {eng: {"tools": [], "routes": [], "do_functions": []} for eng in all_engines}

    for tool in gen.extract_tools(str(_ROOT), "HEAD"):
        engine_data.setdefault(tool["engine"], {"tools": [], "routes": [], "do_functions": []})
        engine_data[tool["engine"]]["tools"].append(tool)
    for route in gen.extract_routes(str(_ROOT), "HEAD"):
        engine_data.setdefault(route["engine"], {"tools": [], "routes": [], "do_functions": []})
        engine_data[route["engine"]]["routes"].append(route)
    for eng in gen.VERTICAL_ENGINES:
        engine_data[eng]["do_functions"] = gen.find_do_functions(
            str(_ROOT), "HEAD", f"nce/vertical_modules/{eng}/"
        )

    lines = ["# Surface of Truth", "", "| Engine | Tools (+ flags) | Routes | Cores (`do_*`) |", "|---|---|---|---|"]
    for eng in all_engines:
        data = engine_data[eng]
        t_str = "<br>".join(
            f"`{t['name']}` ({','.join(t['flags'])})" if t["flags"] else f"`{t['name']}`"
            for t in data["tools"]
        )
        r_str = "<br>".join(f"`{r['path']}` -> `{r['endpoint']}`" for r in data["routes"])
        c_str = "<br>".join(sorted({f"`{c['name']}`" for c in data["do_functions"]}))
        if not t_str and not r_str and not c_str:
            continue
        lines.append(f"| **{eng}** | {t_str or '-'} | {r_str or '-'} | {c_str or '-'} |")
    expected = "\n".join(lines) + "\n"

    actual = _SURFACE_DOC.read_text(encoding="utf-8")
    assert actual.strip() == expected.strip(), (
        "docs/_generated/surface.md does not match scripts/gen_surface_table.py's output for the "
        "current tree -- it was hand-edited or is stale. Regenerate with:\n"
        "  python scripts/gen_surface_table.py --repo . --baseline HEAD --out docs/_generated/surface.md"
    )
