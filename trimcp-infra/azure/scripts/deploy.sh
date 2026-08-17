#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "Deploy subscription-scoped Bicep (requires Contributor at subscription):"
echo "  az deployment sub create --location westeurope --template-file \"$ROOT/main.bicep\" --parameters \"$ROOT/parameters.example.json\""
exit 1
