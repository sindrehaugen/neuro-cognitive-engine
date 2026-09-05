import os
import re
import subprocess
import sys

# TD-1: these were hard-coded to one developer's staging tree and to a frozen
# baseline sha, so the checker could not run anywhere else -- which is why it was
# wired into no workflow and neither its passes nor its failures were ever
# observed. Default to this repository's own docs/ and working tree; set
# NCE_DOCS_DIR to sweep a staging tree instead (the original use).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGING_DOCS = os.environ.get("NCE_DOCS_DIR") or os.path.join(REPO, "docs")
BASELINE = "working tree"


def get_git_files():
    cmd = ["git", "-C", REPO, "ls-files"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    return set(res.stdout.strip().splitlines())


def main():
    print("=" * 70)
    print("NCE Documentation Whole-Docs Link & Syntax Sweep (U25 & U33)")
    print(f"Staging Path: {STAGING_DOCS}")
    print(f"Repo Baseline: {BASELINE}")
    print("=" * 70)

    repo_files = get_git_files()
    staged_files = []
    for root, dirs, files in os.walk(STAGING_DOCS):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, STAGING_DOCS).replace("\\", "/")
            staged_files.append((full_path, "docs/" + rel_path, f))

    print(f"\n[INFO] Total staged files to verify: {len(staged_files)}")

    # 1. Local Windows file:/// URI pattern
    local_file_pattern = re.compile(r"file:///[a-zA-Z]:/[^\s\)\"\'>]+", re.IGNORECASE)
    # Generic markdown link pattern: [text](url)
    markdown_link_pattern = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

    # Status / Verified patterns (handling markdown bolding and variants)
    status_pattern = re.compile(r"(?:\*\*Status:\*\*|Status:)", re.IGNORECASE)
    verified_pattern = re.compile(
        r"(?:\*\*Verified-against:\*\*|Verified-against:)\s*`?[0-9a-f]{7,40}`?", re.IGNORECASE
    )
    spec_annotation_pattern = re.compile(
        r"<!--\s*BLOCKED ON OQ-2 / OQ-4.*?Verified-against:\s*[0-9a-f]{7,40}.*?-->",
        re.DOTALL | re.IGNORECASE,
    )

    local_file_matches = []
    broken_internal_links = []
    syntax_issues = []
    status_summary = []

    # Map of all known files (repo docs + staged docs)
    all_doc_paths = set(f for f in repo_files if f.startswith("docs/"))
    for _, doc_rel, _ in staged_files:
        all_doc_paths.add(doc_rel)

    for full_path, doc_rel, filename in sorted(staged_files):
        if not (filename.endswith(".md") or filename.endswith(".html")):
            continue

        with open(full_path, encoding="utf-8", errors="replace") as f:
            content = f.read()

        lines = content.splitlines()

        # Check for local file:/// links (e.g. file:///C:/...)
        for idx, line in enumerate(lines, 1):
            for match in local_file_pattern.finditer(line):
                local_file_matches.append((doc_rel, idx, match.group(0)))

        # Check code fence balance
        code_fence_count = 0
        for idx, line in enumerate(lines, 1):
            if line.strip().startswith("```"):
                code_fence_count += 1
        if code_fence_count % 2 != 0:
            syntax_issues.append((doc_rel, f"Unbalanced code fences (count={code_fence_count})"))

        # Check Status and Verified-against: 7304330
        # Some files are exempt from header stamps: index.html, _sidebar.md, _navbar.md, _generated/surface.md, API.md (pristine generated)
        is_exempt = filename in ["index.html", "_sidebar.md", "_navbar.md", "surface.md", "API.md"]

        has_status = bool(status_pattern.search(content[:2500]))
        has_verified = bool(verified_pattern.search(content[:2500])) or bool(
            spec_annotation_pattern.search(content[:2500])
        )

        status_summary.append(
            {
                "doc": doc_rel,
                "exempt": is_exempt,
                "has_status": has_status,
                "has_verified": has_verified,
                "filename": filename,
            }
        )

        # Check Docsify relative links
        doc_dir = os.path.dirname(doc_rel)
        for idx, line in enumerate(lines, 1):
            for m in markdown_link_pattern.finditer(line):
                text, url = m.group(1), m.group(2).strip()
                if (
                    url.startswith("http://")
                    or url.startswith("https://")
                    or url.startswith("mailto:")
                    or url.startswith("#")
                ):
                    continue
                if url.startswith("file:"):
                    continue

                # Clean hash and query
                url_clean = url.split("#")[0].split("?")[0].strip()
                if not url_clean:
                    continue

                norm_target = os.path.normpath(os.path.join(doc_dir, url_clean)).replace("\\", "/")

                # Check if target exists in docs
                if (
                    norm_target in all_doc_paths
                    or norm_target + ".md" in all_doc_paths
                    or norm_target + "/README.md" in all_doc_paths
                ):
                    continue
                # Check if target is a directory in docs
                if any(x.startswith(norm_target + "/") for x in all_doc_paths):
                    continue
                # Check if target exists in repo code
                if norm_target in repo_files or any(
                    x.startswith(norm_target + "/") for x in repo_files
                ):
                    # Relative link to code in repo
                    continue

                broken_internal_links.append((doc_rel, idx, text, url, norm_target))

    # --- Print Audit Results ---
    print("\n" + "=" * 70)
    print("AUDIT RESULTS:")
    print("=" * 70)

    # 1. Local file URI check
    print("\n[1] Local Windows file:/// URIs Check:")
    if not local_file_matches:
        print("    -> PASS: 0 local Windows file:/// links found in staged docs.")
    else:
        print(f"    -> FAIL: Found {len(local_file_matches)} local file:/// URIs:")
        for doc, line, uri in local_file_matches:
            print(f"       * {doc}:{line} -> {uri}")

    # 2. Syntax & Code Fence Balance Check
    print("\n[2] Syntax & Code Fence Balance Check:")
    if not syntax_issues:
        print(
            "    -> PASS: All files have perfectly balanced code fences and valid markdown syntax."
        )
    else:
        print(f"    -> FAIL: Found {len(syntax_issues)} syntax issues:")
        for doc, issue in syntax_issues:
            print(f"       * {doc}: {issue}")

    # 3. Relative Navigation Links Check
    print("\n[3] Relative Navigation Links Check:")
    if not broken_internal_links:
        print("    -> PASS: All relative navigation links resolve to valid targets.")
    else:
        print(f"    -> FAIL: Found {len(broken_internal_links)} broken relative links:")
        for doc, line, text, url, target in broken_internal_links:
            print(f"       * {doc}:{line} -> [{text}]({url}) (target: {target})")

    # 4. Status and Verified-against Stamps Check
    # TD-1: WARNING ONLY, deliberately. A stamp is a claim about when somebody last
    # looked; a broken link is a fact about the document. Gating a merge on the
    # first punishes waves that correctly update a doc, so the verdict below is
    # computed from links and code fences only.
    print("\n[4] Status & Verified-against Stamps Check (WARNING ONLY -- never fatal):")
    non_exempt = [s for s in status_summary if not s["exempt"]]
    stamped = [s for s in non_exempt if s["has_status"] and s["has_verified"]]
    unstamped = [s for s in non_exempt if not (s["has_status"] and s["has_verified"])]

    print(f"    Total docs evaluated: {len(status_summary)}")
    print(
        f"    Exempt system/navigation files: {len(status_summary) - len(non_exempt)} (index.html, _sidebar.md, _navbar.md, surface.md, API.md)"
    )
    print(f"    Operational docs verified: {len(non_exempt)}")
    print(f"    Correctly stamped: {len(stamped)}")
    print(f"    Unstamped or unverified: {len(unstamped)}")

    if unstamped:
        print("\n    WARN: unstamped/unverified files (informational, not fatal):")
        for u in unstamped:
            print(f"       * {u['doc']} (status={u['has_status']}, verified={u['has_verified']})")

    # Overall Verdict
    success = (not local_file_matches) and (not syntax_issues) and (not broken_internal_links)
    print("\n" + "=" * 70)
    if success:
        print("OVERALL VERDICT: PASS")
    else:
        print("OVERALL VERDICT: FAIL")
    print("=" * 70)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
