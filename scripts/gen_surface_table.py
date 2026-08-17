import argparse
import ast
import os
import subprocess

VERTICAL_ENGINES = [
    "agreements",
    "diagnostics",
    "dynamics365",
    "economy",
    "inventory",
    "netbox",
    "procurement",
    "product",
    "project",
    "sales",
    "system_design",
    "vendors",
]


def git_ls_tree(repo, baseline, path=""):
    cmd = ["git", "-C", repo, "ls-tree", "-r", "--name-only", baseline, "--", path]
    if not path:
        cmd.pop()
        cmd.pop()
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_show(repo, baseline, path):
    cmd = ["git", "-C", repo, "show", f"{baseline}:{path}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
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
                if isinstance(node, ast.FunctionDef) and node.name.startswith("do_"):
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
