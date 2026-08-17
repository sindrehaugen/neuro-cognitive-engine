#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
terraform init
terraform plan -var-file=terraform.tfvars
echo "Run: terraform apply -var-file=terraform.tfvars"
