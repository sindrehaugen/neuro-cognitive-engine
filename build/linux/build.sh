#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DIST_DIR="${SCRIPT_DIR}/dist"

mkdir -p "${DIST_DIR}"

cd "${REPO_ROOT}/go"

LDFLAGS="-s -w -X github.com/nce/tri-stack/launch.defaultAppRoot=/opt/nce"

echo "Building nce-launch for linux/amd64..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "${LDFLAGS}" -o "${DIST_DIR}/nce-launch-linux-amd64" ./cmd/nce-launch

echo "Building nce-launch for linux/arm64..."
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "${LDFLAGS}" -o "${DIST_DIR}/nce-launch-linux-arm64" ./cmd/nce-launch

echo "Build complete. Artifacts in ${DIST_DIR}:"
ls -la "${DIST_DIR}"
