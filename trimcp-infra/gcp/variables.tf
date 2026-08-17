variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "deployment_name" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type = string
}

variable "vpn_subnet_cidr" {
  type        = string
  default     = "10.100.0.0/24"
  description = "Documented VPN client CIDR (firewall modeling / future peering; Appendix I.6)"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "postgres_tier" {
  type    = string
  default = "db-f1-micro"
}

variable "postgres_disk_gb" {
  type    = number
  default = 50
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "worker_image" {
  type        = string
  description = "Artifact Registry image for RQ worker (replace with TriMCP worker)"
}

variable "webhook_image" {
  type        = string
  default     = "gcr.io/cloudrun/hello"
  description = "Webhook receiver image until FastAPI is published"
}

locals {
  common_labels = merge(var.labels, {
    trimcp_deployment = var.deployment_name
    trimcp_environment = var.environment
  })

  redis_memory = var.environment == "prod" ? 5 : 1
}
