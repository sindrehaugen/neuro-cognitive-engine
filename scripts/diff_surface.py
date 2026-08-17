import difflib

file1 = r"C:\Claude\NCE_DOCS_STAGING\docs\_generated\surface.md"
file2 = r"C:\Claude\NCE_DOCS_STAGING\docs\_generated\surface_repro.md"

with open(file1, encoding="utf-8") as f1, open(file2, encoding="utf-8") as f2:
    lines1 = f1.readlines()
    lines2 = f2.readlines()

diff = list(difflib.unified_diff(lines1, lines2, fromfile="surface.md", tofile="surface_repro.md"))

if not diff:
    print("MATCH: docs/_generated/surface.md is 100% byte-for-byte reproducible!")
else:
    print(f"DIFF FOUND ({len(diff)} lines):")
    for line in diff:
        print(line, end="")
