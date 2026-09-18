import argparse
import ast
import os
import subprocess

VERTICAL_ENGINES: list[str] = []


def discover_vertical_engines(repo, baseline):
    """Every package under ``nce/vertical_modules/``, read from the tree itself.

    This list used to be a hard-coded twelve. Six engines merged on 2026-09-05
    and `assets` before them, and none of them appeared in the generated surface
    table -- their tools and routes were silently attributed to `shared`, which
    is exactly the kind of drift this generator exists to prevent. Deriving the
    list from the tree means a new engine cannot be omitted by forgetting to
    edit this file.
    """
    paths = git_ls_tree(repo, baseline, "nce/vertical_modules/")
    engines = set()
    for path in paths:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "nce" and parts[1] == "vertical_modules":
            if not parts[2].endswith(".py"):
                engines.add(parts[2])
    return sorted(engines)


def git_ls_tree(repo, baseline, path=""):
    cmd = ["git", "-C", repo, "ls-tree", "-r", "--name-only", baseline, "--", path]
    if not path:
        cmd.pop()
        cmd.pop()
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_show(repo, baseline, path):
    cmd = ["git", "-C", repo, "show", f"{baseline}:{path}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return result.stdout


def get_import_map(code):
    tree = ast.parse(code)
    import_map = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                import_map[name] = f"{mod}.{alias.name}" if mod else alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name
                import_map[name] = alias.name
    return import_map


def extract_tools(repo, baseline):
    code = git_show(repo, baseline, "nce/tool_registry.py")
    import_map = get_import_map(code)
    tree = ast.parse(code)
    tools = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "TOOL_REGISTRY":
            if isinstance(node.value, ast.Dict):
                for k, v in zip(node.value.keys, node.value.values):
                    if isinstance(k, ast.Constant):
                        tool_name = k.value
                        flags = []
                        handler_module = None
                        if isinstance(v, ast.Call):
                            for arg in v.args:
                                if (
                                    isinstance(arg, ast.Call)
                                    and getattr(arg.func, "id", None) == "_h"
                                ):
                                    if arg.args:
                                        first_arg = arg.args[0]
                                        if isinstance(first_arg, ast.Name):
                                            handler_module = first_arg.id
                                        elif isinstance(first_arg, ast.Attribute):
                                            if isinstance(first_arg.value, ast.Name):
                                                handler_module = first_arg.value.id
                            for kw in v.keywords:
                                if getattr(kw.value, "value", False) is True:
                                    flags.append(kw.arg)

                        resolved_path = import_map.get(handler_module, handler_module or "")

                        # Engine classification
                        assigned_engine = "shared"
                        for eng in VERTICAL_ENGINES:
                            if (
                                f"vertical_modules.{eng}" in resolved_path
                                or f"vertical_modules/{eng}" in resolved_path
                            ):
                                assigned_engine = eng
                                break

                        # Explicit alias fallback
                        if assigned_engine == "shared":
                            alias_map = {
                                "diag_mcp_handlers": "diagnostics",
                                "d365_mcp_handlers": "dynamics365",
                                "netbox_circuits": "netbox",
                                "netbox_mcp_handlers": "netbox",
                            }
                            if handler_module in alias_map:
                                assigned_engine = alias_map[handler_module]

                        tools.append(
                            {
                                "name": tool_name,
                                "module": handler_module,
                                "resolved_module": resolved_path,
                                "engine": assigned_engine,
                                "flags": flags,
                            }
                        )

    # v1.6 C12 Resource Surface auto-mounted tools (Lane A-1/A-1b, Lane E).
    # nce/tool_registry.py:TOOL_REGISTRY.update(build_all_resource_tool_specs())
    # adds 4 tools (list/get/upsert/archive) per registered ResourceSpec at
    # import time -- invisible to the static-dict AST walk above. Found stale
    # during Lane H janitor pass 6 (K-H3): inventory/notifications/procurement
    # rows were silently missing every C12 tool, notifications showed "-".
    for eng in VERTICAL_ENGINES:
        resources_path = f"nce/vertical_modules/{eng}/resources.py"
        if resources_path not in git_ls_tree(repo, baseline, f"nce/vertical_modules/{eng}/"):
            continue
        try:
            rtree = ast.parse(git_show(repo, baseline, resources_path), filename=resources_path)
        except SyntaxError:
            continue
        for rnode in ast.walk(rtree):
            if isinstance(rnode, ast.Call) and getattr(rnode.func, "id", None) == "ResourceSpec":
                spec_engine, spec_entity = eng, None
                for kw in rnode.keywords:
                    if kw.arg == "engine" and isinstance(kw.value, ast.Constant):
                        spec_engine = str(kw.value.value)
                    elif kw.arg == "entity" and isinstance(kw.value, ast.Constant):
                        spec_entity = str(kw.value.value)
                mcp_slug = (spec_entity or "resource").replace("-", "_").strip("_")
                for op, flag in (
                    ("list", "cacheable"),
                    ("get", "cacheable"),
                    ("upsert", "mutation"),
                    ("archive", "mutation"),
                ):
                    tools.append(
                        {
                            "name": f"{spec_engine}_{op}_{mcp_slug}",
                            "module": "resource_surface",
                            "resolved_module": "nce.resource_surface.mcp",
                            "engine": spec_engine,
                            "flags": [flag],
                        }
                    )
    return tools


def extract_routes(repo, baseline):
    code = git_show(repo, baseline, "nce/admin_app.py")
    import_map = get_import_map(code)
    tree = ast.parse(code)
    routes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_admin_routes":
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.List):
                    for elt in stmt.value.elts:
                        if isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "Route":
                            path = ""
                            if elt.args and isinstance(elt.args[0], ast.Constant):
                                path = elt.args[0].value
                            endpoint = None
                            handler_mod = None
                            for kw in elt.keywords:
                                if kw.arg == "endpoint":
                                    if isinstance(kw.value, ast.Attribute):
                                        if isinstance(kw.value.value, ast.Name):
                                            endpoint = f"{kw.value.value.id}.{kw.value.attr}"
                                            handler_mod = kw.value.value.id
                                    elif isinstance(kw.value, ast.Name):
                                        endpoint = kw.value.id
                                        handler_mod = kw.value.id

                            resolved_mod = import_map.get(handler_mod, handler_mod or "")

                            # Engine attribution: prioritize handler module, then route prefix
                            assigned_engine = "shared"
                            for eng in VERTICAL_ENGINES:
                                if (
                                    f".admin_handlers.{eng}" in resolved_mod
                                    or f"vertical_modules.{eng}" in resolved_mod
                                ):
                                    assigned_engine = eng
                                    break

                            if assigned_engine == "shared":
                                if resolved_mod.startswith("nce.admin_handlers.sales"):
                                    assigned_engine = "sales"
                                elif (
                                    path.startswith("/public-api/sales")
                                    or path.startswith("/api/sales")
                                    or path.startswith("/api/admin/sales")
                                ):
                                    assigned_engine = "sales"
                                elif path.startswith("/api/system-design"):
                                    assigned_engine = "system_design"

                            routes.append(
                                {
                                    "path": path,
                                    "endpoint": endpoint,
                                    "handler_mod": handler_mod,
                                    "resolved_mod": resolved_mod,
                                    "engine": assigned_engine,
                                }
                            )

    # v1.6 C12 Resource Surface auto-mounted routes (Lane A-1/A-1b, Lane E).
    # build_admin_routes() splices `*build_all_resource_routes()` into its
    # return list (nce/admin_app.py) -- a Starred call, not a literal Route(...)
    # element, so the walk above never sees it. Found stale alongside the tool
    # gap in janitor pass 6 (K-H3): every C12 resource's 13 REST routes
    # (list/create/bulk/get/patch/archive/restore/events/comments x2/tags x2)
    # were missing from every engine's row. Path shape is read directly from
    # ResourceSpec.rest_collection_path/rest_item_path (spec.py): derived from
    # (engine, entity) alone, not re-guessed here.
    for eng in VERTICAL_ENGINES:
        resources_path = f"nce/vertical_modules/{eng}/resources.py"
        if resources_path not in git_ls_tree(repo, baseline, f"nce/vertical_modules/{eng}/"):
            continue
        try:
            rtree = ast.parse(git_show(repo, baseline, resources_path), filename=resources_path)
        except SyntaxError:
            continue
        for rnode in ast.walk(rtree):
            if isinstance(rnode, ast.Call) and getattr(rnode.func, "id", None) == "ResourceSpec":
                spec_engine, spec_entity = eng, None
                for kw in rnode.keywords:
                    if kw.arg == "engine" and isinstance(kw.value, ast.Constant):
                        spec_engine = str(kw.value.value)
                    elif kw.arg == "entity" and isinstance(kw.value, ast.Constant):
                        spec_entity = str(kw.value.value)
                rest_slug = (spec_entity or "resource").replace("_", "-").strip("-")
                prefix = f"/api/{spec_engine}/{rest_slug}"
                for path, op in (
                    (prefix, "handle_list"),
                    (prefix, "handle_create"),
                    (f"{prefix}/bulk", "handle_bulk"),
                    (f"{prefix}/{{id}}", "handle_get"),
                    (f"{prefix}/{{id}}", "handle_patch"),
                    (f"{prefix}/{{id}}/archive", "handle_archive"),
                    (f"{prefix}/{{id}}/restore", "handle_restore"),
                    (f"{prefix}/{{id}}/events", "handle_events"),
                    (f"{prefix}/{{id}}/comments", "handle_list_comments"),
                    (f"{prefix}/{{id}}/comments", "handle_add_comment"),
                    (f"{prefix}/{{id}}/tags", "handle_list_tags"),
                    (f"{prefix}/{{id}}/tags", "handle_add_tag"),
                    (f"{prefix}/{{id}}/tags/{{tag}}", "handle_remove_tag"),
                ):
                    routes.append(
                        {
                            "path": path,
                            "endpoint": f"resource_surface.{op}",
                            "handler_mod": "resource_surface",
                            "resolved_mod": "nce.resource_surface.rest",
                            "engine": spec_engine,
                        }
                    )
    return routes


def find_do_functions(repo, baseline, engine_dir):
    files = git_ls_tree(repo, baseline, engine_dir)
    do_functions = []
    for f in files:
        if not f.endswith(".py"):
            continue
        try:
            code = git_show(repo, baseline, f)
            tree = ast.parse(code)
            for node in ast.walk(tree):
                # ast.FunctionDef alone missed every `async def do_*` core, which is
                # most of them: this column read 0 for eleven engines that have dozens.
                # A generated table that undercounts is worse than none, because it
                # gets quoted as a measurement.
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and node.name.startswith("do_"):
                    do_functions.append({"name": node.name, "file": f})
        except Exception:
            pass
    return do_functions


def main():
    parser = argparse.ArgumentParser(
        description="Generate Surface of Truth table from NCE codebase."
    )
    parser.add_argument("--repo", required=True, help="Path to NCE git repo")
    parser.add_argument("--baseline", required=True, help="Git baseline commit SHA")
    parser.add_argument("--out", required=True, help="Output markdown path")
    args = parser.parse_args()

    global VERTICAL_ENGINES
    VERTICAL_ENGINES = discover_vertical_engines(args.repo, args.baseline)
    all_engines = list(VERTICAL_ENGINES) + ["shared"]
    engine_data = {eng: {"tools": [], "routes": [], "do_functions": []} for eng in all_engines}

    tools = extract_tools(args.repo, args.baseline)
    for tool in tools:
        eng = tool["engine"]
        if eng not in engine_data:
            engine_data[eng] = {"tools": [], "routes": [], "do_functions": []}
        engine_data[eng]["tools"].append(tool)

    routes = extract_routes(args.repo, args.baseline)
    for route in routes:
        eng = route["engine"]
        if eng not in engine_data:
            engine_data[eng] = {"tools": [], "routes": [], "do_functions": []}
        engine_data[eng]["routes"].append(route)

    for eng in VERTICAL_ENGINES:
        eng_dir = f"nce/vertical_modules/{eng}/"
        funcs = find_do_functions(args.repo, args.baseline, eng_dir)
        engine_data[eng]["do_functions"] = funcs

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("# Surface of Truth\n\n")
        f.write("| Engine | Tools (+ flags) | Routes | Cores (`do_*`) |\n")
        f.write("|---|---|---|---|\n")

        for eng in all_engines:
            data = engine_data[eng]
            t_str = "<br>".join(
                [
                    f"`{t['name']}` ({','.join(t['flags'])})" if t['flags'] else f"`{t['name']}`"
                    for t in data["tools"]
                ]
            )
            r_str = "<br>".join([f"`{r['path']}` -> `{r['endpoint']}`" for r in data["routes"]])
            c_str = "<br>".join(sorted(list(set(f"`{c['name']}`" for c in data["do_functions"]))))

            if not t_str:
                t_str = "-"
            if not r_str:
                r_str = "-"
            if not c_str:
                c_str = "-"

            f.write(f"| **{eng}** | {t_str} | {r_str} | {c_str} |\n")


if __name__ == "__main__":
    main()
