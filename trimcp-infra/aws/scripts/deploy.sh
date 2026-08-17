#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
terraform init
terraform plan -var-file=terraform.tfvars.example
echo "Copy terraform.tfvars.example to terraform.tfvars, edit, then: terraform apply -var-file=terraform.tfvars"
