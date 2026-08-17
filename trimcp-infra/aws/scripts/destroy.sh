#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Uncomment after confirming resources to destroy."
# terraform destroy -var-file=terraform.tfvars
exit 1
