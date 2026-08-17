#!/usr/bin/env bash
# Render TriMCP client .env from IaC outputs + cloud secret manager (Appendix I.1 / I.7).
# Usage:
#   ./scripts/render-env.sh --cloud aws --infra-dir trimcp-infra/aws [--json-file tf-out.json]
#   ./scripts/render-env.sh --cloud gcp --infra-dir trimcp-infra/gcp
#   ./scripts/render-env.sh --cloud azure --infra-dir trimcp-infra/azure --json-file deployment-out.json
#
# Requires: python3, jinja2 (`python3 -m pip install jinja2`)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$ROOT/trimcp-infra/shared/client-env-template.j2"
CLOUD=""
INFRADIR_REL=""
JSON_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cloud) CLOUD="${2:-}" ; shift 2 ;;
    --infra-dir) INFRADIR_REL="${2:-}" ; shift 2 ;;
    --json-file) JSON_FILE="${2:-}" ; shift 2 ;;
    *) echo "Unknown arg: $1" >&2 ; exit 2 ;;
  esac
done

if [[ -z "$CLOUD" || -z "$INFRADIR_REL" ]]; then
  echo "Usage: $0 --cloud aws|gcp|azure --infra-dir <path> [--json-file outputs.json]" >&2
  exit 2
fi

INFRADIR="$ROOT/$INFRADIR_REL"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "Missing template: $TEMPLATE" >&2
  exit 1
fi

if [[ "$CLOUD" == "azure" && -z "$JSON_FILE" ]]; then
  echo "Azure: provide ARM JSON with --json-file (export from portal or az deployment show)." >&2
  exit 1
fi

collect_tf_json() {
  local dir="$1"
  if [[ -n "${JSON_FILE:-}" ]]; then
    cat "$JSON_FILE"
    return
  fi
  if [[ -f "$dir/terraform.tfstate" ]] || [[ -d "$dir/.terraform" ]]; then
    (cd "$dir" && terraform output -json)
    return
  fi
  echo "No terraform state in $dir; pass --json-file with output JSON." >&2
  exit 1
}

CTX_JSON=$(mktemp)
trap 'rm -f "$CTX_JSON"' EXIT

case "$CLOUD" in
  aws|gcp) collect_tf_json "$INFRADIR" >"$CTX_JSON" ;;
  azure) cat "$JSON_FILE" >"$CTX_JSON" ;;
  *) echo "Unknown --cloud $CLOUD" >&2 ; exit 2 ;;
esac

export TRIMCP_RENDER_CLOUD="$CLOUD"

python3 "$ROOT/scripts/_render_env.py" "$TEMPLATE" "$CTX_JSON" "$CLOUD"

echo ""
echo "# --- Optional: Docker Compose dev secret bootstrap (local stack) ---"
echo "#   python3 \"$ROOT/scripts/bootstrap-compose-secrets.py\""
echo "#   (writes deploy/compose.stack.env.generated; loaded after compose.stack.env)"
