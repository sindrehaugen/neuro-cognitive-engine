import os

STAGING_DOCS = r"C:\Claude\NCE_DOCS_STAGING\docs"


def main():
    files = []
    for root, dirs, filenames in os.walk(STAGING_DOCS):
        for f in filenames:
            if f.endswith(".md") or f.endswith(".html"):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, STAGING_DOCS).replace("\\", "/")
                files.append((full, rel, f))

    print(f"Total files: {len(files)}")

    for full, rel, fname in sorted(files):
        with open(full, encoding="utf-8", errors="replace") as fh:
            content = fh.read()

        # Check first 1000 chars for status/verified
        header = content[:1000]
        has_status = "status:" in header.lower() or "status" in header.lower()
        has_7304330 = "7304330" in header

        print(f"File: {rel}")
        # print first 2 lines
        first_lines = [line for line in content.splitlines()[:5] if line.strip()]
        for line in first_lines[:2]:
            print(f"   {line[:100]}")
        print(f"   -> has_status={has_status}, has_7304330={has_7304330}")
        print()


if __name__ == "__main__":
    main()
