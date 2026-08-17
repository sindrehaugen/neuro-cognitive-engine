#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "DANGER: destroys managed resources. Uncomment in script after review."
# terraform destroy -var-file=terraform.tfvars
exit 1
