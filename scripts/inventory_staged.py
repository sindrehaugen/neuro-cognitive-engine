import os

STAGING_DIR = r"C:\Claude\NCE_DOCS_STAGING"


def main():
    all_files = []
    for root, dirs, files in os.walk(STAGING_DIR):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, STAGING_DIR).replace("\\", "/")
            size = os.path.getsize(full)
            all_files.append((rel, size))

    print(f"Total files in staging: {len(all_files)}")
    for rel, size in sorted(all_files):
        print(f"{rel} ({size} bytes)")


if __name__ == "__main__":
    main()
