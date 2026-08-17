import os
import re

STAGING_DOCS = r"C:\Claude\NCE_DOCS_STAGING\docs"


def main():
    pat = re.compile(r"FINDINGS_OQ\d[^\s\)\"\'>]*")
    for root, dirs, files in os.walk(STAGING_DOCS):
        for f in files:
            p = os.path.join(root, f)
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for idx, line in enumerate(text.splitlines(), 1):
                m = pat.search(line)
                if m:
                    rel = os.path.relpath(p, STAGING_DOCS).replace("\\", "/")
                    print(f"{rel}:{idx} -> {line.strip()}")


if __name__ == "__main__":
    main()
